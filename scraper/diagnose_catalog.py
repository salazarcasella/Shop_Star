from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

URL = "https://www.shopstar.pe/bombas-millas"
OUTPUT = Path("data/diagnostic_catalog.json")


def product_like_count(obj: Any) -> int:
    if isinstance(obj, dict):
        here = int(("productName" in obj or "productId" in obj) and ("items" in obj or "link" in obj or "linkText" in obj))
        return here + sum(product_like_count(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(product_like_count(v) for v in obj)
    return 0


async def main() -> None:
    report: dict[str, Any] = {"source_url": URL, "snapshots": [], "responses": []}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="es-PE",
            timezone_id="America/Lima",
            viewport={"width": 1440, "height": 1600},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
        )
        page = await context.new_page()
        seen_urls: set[str] = set()
        pending: list[asyncio.Task] = []

        async def capture(response):
            ctype = (response.headers.get("content-type") or "").lower()
            u = response.url
            low = u.lower()
            if u in seen_urls:
                return
            if not ("json" in ctype or any(x in low for x in ("search", "product", "catalog", "graphql", "vtex", "collection"))):
                return
            seen_urls.add(u)
            row: dict[str, Any] = {
                "status": response.status,
                "content_type": ctype[:120],
                "url": u,
            }
            if "json" in ctype:
                try:
                    data = await response.json()
                    row["product_like_nodes"] = product_like_count(data)
                    if isinstance(data, dict):
                        row["top_level_keys"] = list(data.keys())[:40]
                except Exception as exc:
                    row["json_error"] = str(exc)[:300]
            report["responses"].append(row)

        page.on("response", lambda r: pending.append(asyncio.create_task(capture(r))))
        await page.goto(URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(5000)

        async def snapshot(label: str) -> None:
            data = await page.evaluate(r"""() => {
              const norm = s => (s || '').replace(/\s+/g, ' ').trim();
              const productUrls = [...new Set([...document.querySelectorAll('a[href*="/p"]')]
                .map(a => (a.href || a.getAttribute('href') || '').split('?')[0].split('#')[0].replace(/\/$/, ''))
                .filter(u => /\/p$/i.test(u)))];
              const candidates = [...document.querySelectorAll('button,a,[role="button"],div,span')]
                .map(el => ({tag: el.tagName, text: norm(el.innerText || el.textContent), cls: norm(el.className), aria: el.getAttribute('aria-label') || ''}))
                .filter(x => /(mostrar|ver|cargar|más|mas|producto)/i.test(x.text) && x.text.length <= 120)
                .slice(0, 120);
              const resourceUrls = performance.getEntriesByType('resource')
                .map(x => x.name)
                .filter(Boolean)
                .filter(u => /(search|product|catalog|graphql|vtex|collection)/i.test(u))
                .slice(-250);
              const scripts = [...document.scripts].map(s => s.src).filter(Boolean).slice(0, 120);
              return {
                title: document.title,
                href: location.href,
                bodyPrefix: norm(document.body?.innerText || '').slice(0, 1200),
                productCount: productUrls.length,
                productUrls,
                candidates,
                resourceUrls,
                scripts
              };
            }""")
            data["label"] = label
            report["snapshots"].append(data)

        await snapshot("INITIAL")

        for i in range(8):
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
            await page.wait_for_timeout(1800)
            await snapshot(f"AFTER_{i + 1}")
            report.setdefault("clicks", []).append(clicked)
            if not clicked.get("clicked"):
                break

        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        report["response_count"] = len(report["responses"])
        report["max_dom_products"] = max((x.get("productCount", 0) for x in report["snapshots"]), default=0)
        await context.close()
        await browser.close()

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(OUTPUT),
        "max_dom_products": report["max_dom_products"],
        "response_count": report["response_count"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
