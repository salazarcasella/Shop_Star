from __future__ import annotations

import asyncio
import json
import re
from playwright.async_api import async_playwright

URL = "https://www.shopstar.pe/bombas-millas"


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="es-PE",
            timezone_id="America/Lima",
            viewport={"width": 1440, "height": 1600},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
        )
        page = await context.new_page()
        interesting: list[dict[str, str | int]] = []

        async def capture(response):
            ctype = (response.headers.get("content-type") or "").lower()
            u = response.url
            if any(x in u.lower() for x in ("search", "product", "catalog", "graphql", "vtex")) or "json" in ctype:
                interesting.append({"status": response.status, "content_type": ctype[:80], "url": u})

        page.on("response", lambda r: asyncio.create_task(capture(r)))
        await page.goto(URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(5000)

        async def snapshot(label: str):
            data = await page.evaluate(r"""() => {
              const norm = s => (s || '').replace(/\s+/g, ' ').trim();
              const productUrls = [...new Set([...document.querySelectorAll('a[href*="/p"]')]
                .map(a => (a.href || a.getAttribute('href') || '').split('?')[0].split('#')[0].replace(/\/$/, ''))
                .filter(u => /\/p$/i.test(u)))];
              const candidates = [...document.querySelectorAll('button,a,[role="button"],div,span')]
                .map(el => ({tag: el.tagName, text: norm(el.innerText || el.textContent), cls: norm(el.className), aria: el.getAttribute('aria-label') || ''}))
                .filter(x => /(mostrar|ver|cargar|más|mas|producto)/i.test(x.text) && x.text.length <= 120)
                .slice(0, 120);
              const scripts = [...document.scripts].map(s => s.src).filter(Boolean).slice(0, 80);
              return {title: document.title, href: location.href, bodyPrefix: norm(document.body?.innerText || '').slice(0, 800), productCount: productUrls.length, candidates, scripts};
            }""")
            print(f"\n=== {label} ===")
            print(json.dumps(data, ensure_ascii=False, indent=2))

        await snapshot("INITIAL")

        for i in range(6):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1000)
            clicked = await page.evaluate(r"""() => {
              const norm=s=>(s||'').replace(/\s+/g,' ').trim().toLowerCase();
              const rx=/^(mostrar|ver|cargar)\s+m[aá]s(?:\s+productos?)?$|^m[aá]s\s+productos?$/i;
              const els=[...document.querySelectorAll('button,a,[role="button"],div,span')];
              for (const el of els) {
                const t=norm(el.innerText||el.textContent);
                if (!rx.test(t)) continue;
                const r=el.getBoundingClientRect(), st=getComputedStyle(el);
                if (r.width<2||r.height<2||st.display==='none'||st.visibility==='hidden') continue;
                el.scrollIntoView({block:'center'});
                el.click();
                return {clicked:true, tag:el.tagName, text:t, cls:String(el.className||'')};
              }
              return {clicked:false};
            }""")
            print(f"CLICK {i+1}: {json.dumps(clicked, ensure_ascii=False)}")
            await page.wait_for_timeout(1800)
            await snapshot(f"AFTER_{i+1}")
            if not clicked.get("clicked"):
                break

        print("\n=== INTERESTING RESPONSES ===")
        dedup = []
        seen = set()
        for item in interesting:
            key = item["url"]
            if key not in seen:
                seen.add(key)
                dedup.append(item)
        print(json.dumps(dedup[-150:], ensure_ascii=False, indent=2))
        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
