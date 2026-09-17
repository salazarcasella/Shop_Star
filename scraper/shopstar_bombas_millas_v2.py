from __future__ import annotations

import asyncio
import json
import math
import os
import re
from copy import deepcopy
from typing import Any

from playwright.async_api import BrowserContext, Page, async_playwright

from .shopstar_bombas_millas import (
    SOURCE_URL,
    clean as clean_text,
    extract_miles_values,
    extract_soles_values,
    finalize as finalize_product,
    from_dom as build_dom_only_product,
    merge_dom as merge_dom_data,
    normalize_network as normalize_network_product,
    now_iso as utc_now_iso,
    product_nodes as iter_product_nodes,
    url_key as product_path_key,
    write_outputs,
)

HEADLESS = os.getenv("HEADLESS", "1") != "0"
DETAIL_CONCURRENCY = int(os.getenv("DETAIL_CONCURRENCY", "6"))
DETAIL_LIMIT = int(os.getenv("DETAIL_LIMIT", "300"))
MIN_EXPECTED_PRODUCTS = int(os.getenv("MIN_EXPECTED_PRODUCTS", "100"))
MIN_CATALOG_COVERAGE = float(os.getenv("MIN_CATALOG_COVERAGE", "0.95"))
MIN_RATIO_COVERAGE = float(os.getenv("MIN_RATIO_COVERAGE", "0.70"))

SHOW_MORE_TEXT = re.compile(r"^(?:mostrar|ver|cargar)\s+m[aá]s(?:\s+productos?)?$|^m[aá]s\s+productos?$", re.I)
TOTAL_PRODUCTS_RE = re.compile(r"\b([0-9][0-9.,]*)\s+PRODUCTOS\b", re.I)


async def unique_product_count(page: Page) -> int:
    return await page.eval_on_selector_all(
        'a[href*="/p"]',
        """(a) => new Set(a.map(x => (x.href || x.getAttribute('href') || '').split('?')[0].split('#')[0].replace(/\\/$/, '')).filter(x => /\\/p$/i.test(x))).size""",
    )


async def declared_product_total(page: Page) -> int | None:
    try:
        text = clean_text(await page.locator("body").inner_text(timeout=10000)) or ""
    except Exception:
        return None
    m = TOTAL_PRODUCTS_RE.search(text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


async def click_show_more(page: Page, before_count: int) -> dict[str, Any]:
    selectors = [
        "div.vtex-search-result-3-x-buttonShowMore a",
        "div[class*='vtex-search-result-3-x-buttonShowMore'] a",
        "a.vtex-button",
        "a",
    ]

    for selector in selectors:
        locator = page.locator(selector).filter(has_text=SHOW_MORE_TEXT)
        total = await locator.count()
        for idx in range(total - 1, -1, -1):
            el = locator.nth(idx)
            try:
                if not await el.is_visible(timeout=300):
                    continue
                text = clean_text(await el.inner_text(timeout=500)) or ""
                if not SHOW_MORE_TEXT.match(text):
                    continue
                href = await el.get_attribute("href")
                await el.scroll_into_view_if_needed(timeout=1500)
                await el.click(timeout=4000)
                try:
                    await page.wait_for_function(
                        """(n) => new Set([...document.querySelectorAll('a[href*=\"/p\"]')].map(a => (a.href || a.getAttribute('href') || '').split('?')[0].split('#')[0].replace(/\\/$/, '')).filter(u => /\\/p$/i.test(u))).size > n""",
                        arg=before_count,
                        timeout=7000,
                    )
                except Exception:
                    await page.wait_for_timeout(900)
                after = await unique_product_count(page)
                return {
                    "clicked": True,
                    "selector": selector,
                    "text": text,
                    "href": href,
                    "before": before_count,
                    "after": after,
                    "grew": after > before_count,
                }
            except Exception:
                continue

    return {"clicked": False, "before": before_count, "after": before_count, "grew": False}


async def load_full_catalog(page: Page) -> dict[str, Any]:
    declared_total = await declared_product_total(page)
    best = await unique_product_count(page)
    trace = [best]
    attempts: list[dict[str, Any]] = []
    stable = 0

    for _ in range(80):
        if declared_total and best >= declared_total:
            break

        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(700)
        before = await unique_product_count(page)
        result = await click_show_more(page, before)
        attempts.append(result)

        if not result["clicked"]:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(900)

        count = await unique_product_count(page)
        trace.append(count)
        if count > best:
            best = count
            stable = 0
        else:
            stable += 1

        if result["clicked"] and not result["grew"]:
            stable += 1

        if stable >= 5:
            break

    expected_for_gate = declared_total or MIN_EXPECTED_PRODUCTS
    required = min(expected_for_gate, max(MIN_EXPECTED_PRODUCTS, math.ceil(expected_for_gate * MIN_CATALOG_COVERAGE))) if declared_total else MIN_EXPECTED_PRODUCTS
    return {
        "declared_product_total": declared_total,
        "unique_product_links": best,
        "required_product_count": required,
        "show_more_clicks": sum(1 for x in attempts if x.get("clicked")),
        "show_more_growth_clicks": sum(1 for x in attempts if x.get("grew")),
        "coverage_trace": trace,
        "show_more_attempts": attempts[-20:],
    }


async def extract_dom_cards(page: Page) -> list[dict[str, Any]]:
    return await page.eval_on_selector_all('a[href*="/p"]', r"""(anchors)=>{
      const out=[],seen=new Set();
      const norm=s=>(s||'').replace(/\s+/g,' ').trim();
      for(const a of anchors){
        let href=(a.href||a.getAttribute('href')||'').split('?')[0].split('#')[0].replace(/\/$/,'');
        if(!href||!/\/p$/i.test(href)||seen.has(href)) continue;

        let selected=a.closest('[class*="vtex-product-summary-2-x-container"], [class*="vtex-product-summary-2-x-element"]');
        if(!selected){
          let node=a.parentElement||a, best=null, bestScore=-1;
          for(let i=0;i<8&&node;i++,node=node.parentElement){
            const text=norm(node.innerText);
            const links=new Set([...node.querySelectorAll('a[href*="/p"]')].map(x=>(x.href||x.getAttribute('href')||'').split('?')[0].split('#')[0]).filter(x=>/\/p$/i.test(x))).size;
            if(text.length<15||text.length>1800||links!==1) continue;
            const score=(/Millas?|Benefit/i.test(text)?5:0)+(/S\//.test(text)?4:0)+(/Vendido por/i.test(text)?2:0)+(/Agregar|No disponible/i.test(text)?1:0);
            if(score>bestScore){best=node;bestScore=score;}
          }
          selected=best||a.parentElement||a;
        }

        const productLinks=new Set([...selected.querySelectorAll('a[href*="/p"]')].map(x=>(x.href||x.getAttribute('href')||'').split('?')[0].split('#')[0]).filter(x=>/\/p$/i.test(x)));
        if(productLinks.size>1) continue;

        const img=selected.querySelector('img')||a.querySelector('img');
        const text=norm(selected.innerText);
        const nameEl=selected.querySelector('[class*="vtex-product-summary-2-x-productBrand"], [class*="vtex-product-summary-2-x-brandName"], [aria-label^="Nombre del producto"]');
        const anchor=norm(a.getAttribute('aria-label')||a.innerText||img?.alt||'');
        const title=norm(nameEl?.innerText||nameEl?.getAttribute('aria-label')||anchor||text.split(/Vendido por|\d[\d., ]*\s+Millas?|S\//i)[0]);
        seen.add(href);
        out.push({url:href,title,text:text.length<=1800?text:'',image:img?.currentSrc||img?.src||null});
      }
      return out;
    }""")


def _float(v: Any) -> float | None:
    try:
        n = float(v)
        return round(n, 2) if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def _walk(v: Any):
    if isinstance(v, dict):
        yield v
        for x in v.values():
            yield from _walk(x)
    elif isinstance(v, list):
        for x in v:
            yield from _walk(x)


def jsonld_fields(documents: list[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    prices: list[float] = []
    highs: list[float] = []
    for doc in documents:
        for node in _walk(doc):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if "Product" in types:
                out["name"] = out.get("name") or clean_text(node.get("name"))
                brand = node.get("brand")
                brand = brand.get("name") if isinstance(brand, dict) else brand
                out["brand"] = out.get("brand") or clean_text(brand)
                out["sku_id"] = out.get("sku_id") or clean_text(node.get("sku"))
                out["ean"] = out.get("ean") or clean_text(node.get("gtin13") or node.get("gtin") or node.get("gtin14"))
                image = node.get("image")
                image = next((x for x in image if isinstance(x, str)), None) if isinstance(image, list) else image
                out["image"] = out.get("image") or clean_text(image)
            if "Offer" in types or "AggregateOffer" in types or "UnitPriceSpecification" in types:
                for k in ("price", "lowPrice"):
                    n = _float(node.get(k))
                    if n and 0 < n < 10_000_000:
                        prices.append(n)
                n = _float(node.get("highPrice"))
                if n and 0 < n < 10_000_000:
                    highs.append(n)
                av = clean_text(node.get("availability"))
                if av:
                    out["available"] = "OutOfStock" not in av
    if prices:
        out["price_soles"] = min(prices)
    if highs:
        out["list_price_soles"] = max(highs)
    return out


async def enrich_detail(context: BrowserContext, product: dict[str, Any], sem: asyncio.Semaphore) -> None:
    if (product.get("miles_benefit") and product.get("price_soles")) or not product.get("url"):
        return

    async with sem:
        page = await context.new_page()
        try:
            await page.goto(product["url"], wait_until="domcontentloaded", timeout=50000)
            await page.wait_for_timeout(1300)

            docs = []
            for raw in await page.locator('script[type="application/ld+json"]').all_text_contents():
                try:
                    docs.append(json.loads(raw))
                except Exception:
                    pass
            st = jsonld_fields(docs)
            for k in ("name", "brand", "sku_id", "ean", "image", "available"):
                if not product.get(k) and st.get(k) is not None:
                    product[k] = st[k]
            if product.get("price_soles") is None and st.get("price_soles"):
                product["price_soles"] = st["price_soles"]
                product["price_source"] = "jsonld"
            if product.get("list_price_soles") is None and st.get("list_price_soles"):
                product["list_price_soles"] = st["list_price_soles"]

            body = clean_text(await page.locator("body").inner_text(timeout=10000)) or ""
            miles, soles = extract_miles_values(body), extract_soles_values(body)
            if not product.get("miles_benefit") and miles:
                product["miles_benefit"] = min(miles)
                product["miles_reference"] = max(miles) if len(miles) > 1 else product.get("miles_reference")
                product["miles_source"] = "product_detail"
            if product.get("price_soles") is None and soles:
                product["price_soles"] = min(soles)
                product["price_source"] = "product_detail_text"
            if product.get("list_price_soles") is None and len(soles) > 1:
                product["list_price_soles"] = max(soles)

            product["detail_text_excerpt"] = body[:1200] or None
            product["detail_enriched"] = True
        except Exception as exc:
            product["detail_error"] = (clean_text(exc) or type(exc).__name__)[:500]
        finally:
            await page.close()


async def scrape() -> dict[str, Any]:
    captured: list[dict[str, Any]] = []
    tasks: list[asyncio.Task] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(
            locale="es-PE",
            timezone_id="America/Lima",
            viewport={"width": 1440, "height": 1600},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
        )
        page = await context.new_page()

        async def capture(response):
            ctype = (response.headers.get("content-type") or "").lower()
            low = response.url.lower()
            if "json" not in ctype:
                return
            if not any(x in low for x in ("shopstar.pe", "vtex", "search", "product", "catalog", "graphql")):
                return
            try:
                data = await response.json()
                captured.extend(deepcopy(x) for x in iter_product_nodes(data))
            except Exception:
                pass

        page.on("response", lambda r: tasks.append(asyncio.create_task(capture(r))))
        await page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(3500)

        coverage = await load_full_catalog(page)
        await page.wait_for_timeout(1000)
        cards = await extract_dom_cards(page)

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        net: dict[str, dict[str, Any]] = {}
        for raw in captured:
            p = normalize_network_product(raw)
            k = product_path_key(p.get("url"))
            if not k:
                continue
            previous = net.get(k)
            if previous is None or (previous.get("price_soles") is None and p.get("price_soles") is not None):
                net[k] = p

        dom = {product_path_key(x.get("url")): x for x in cards if product_path_key(x.get("url"))}
        products: list[dict[str, Any]] = []
        for k in dict.fromkeys([*dom.keys(), *net.keys()]):
            if not k:
                continue
            if k in net:
                p = net[k]
                merge_dom_data(p, dom.get(k))
            else:
                p = build_dom_only_product(dom[k])
            products.append(p)

        missing = [p for p in products if not (p.get("miles_benefit") and p.get("price_soles"))][:DETAIL_LIMIT]
        sem = asyncio.Semaphore(max(1, DETAIL_CONCURRENCY))
        await asyncio.gather(*(enrich_detail(context, p, sem) for p in missing))

        await context.close()
        await browser.close()

    for p in products:
        finalize_product(p)
    products.sort(key=lambda p: (p.get("soles_per_mile") is not None, p.get("soles_per_mile") or -1), reverse=True)

    ratio_count = sum(1 for p in products if p.get("soles_per_mile") is not None)
    price_count = sum(1 for p in products if p.get("price_soles"))
    miles_count = sum(1 for p in products if p.get("miles_benefit"))
    meta = {
        "source_url": SOURCE_URL,
        "scraped_at": utc_now_iso(),
        "product_count": len(products),
        "products_with_price": price_count,
        "products_with_miles": miles_count,
        "products_with_ratio": ratio_count,
        "ratio_coverage": round(ratio_count / len(products), 4) if products else 0.0,
        "network_products_captured": len(captured),
        "network_products_merged": len(net),
        "dom_product_cards": len(cards),
        **coverage,
        "detail_pages_requested": len(missing),
        "detail_pages_enriched": sum(1 for p in products if p.get("detail_enriched")),
        "detail_errors": sum(1 for p in products if p.get("detail_error")),
        "ratio_definition": "soles_per_mile = current cash price in PEN / displayed Benefit miles",
        "best_ratio_rule": "Higher soles_per_mile (or soles_per_1000_miles) means more PEN value extracted per Benefit mile.",
        "scraper_version": "2.1.0",
    }
    return {"metadata": meta, "products": products}


def validate_payload(payload: dict[str, Any]) -> None:
    meta = payload["metadata"]
    count = int(meta.get("product_count") or 0)
    required = int(meta.get("required_product_count") or MIN_EXPECTED_PRODUCTS)
    if count < required:
        declared = meta.get("declared_product_total")
        raise SystemExit(
            f"Coverage check failed: found {count} products, required at least {required}"
            + (f" of {declared} announced by Shopstar" if declared else "")
            + "; existing JSON remains untouched."
        )

    ratio_coverage = float(meta.get("ratio_coverage") or 0)
    if ratio_coverage < MIN_RATIO_COVERAGE:
        raise SystemExit(
            f"Data quality check failed: only {meta.get('products_with_ratio', 0)}/{count} products "
            f"({ratio_coverage:.1%}) have both price and Benefit miles; minimum is {MIN_RATIO_COVERAGE:.0%}. "
            "Existing JSON remains untouched."
        )


def main() -> None:
    payload = asyncio.run(scrape())
    validate_payload(payload)
    write_outputs(payload)
    print(json.dumps(payload["metadata"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
