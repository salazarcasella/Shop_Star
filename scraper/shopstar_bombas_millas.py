from __future__ import annotations

import asyncio
import json
import math
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright

SOURCE_URL = os.getenv("SHOPSTAR_URL", "https://www.shopstar.pe/bombas-millas")
OUTPUT_PATH = Path(os.getenv("OUTPUT_PATH", "data/latest.json"))
HISTORY_PATH = Path(os.getenv("HISTORY_PATH", "data/history.json"))
MAX_HISTORY_RUNS = int(os.getenv("MAX_HISTORY_RUNS", "90"))
DETAIL_CONCURRENCY = int(os.getenv("DETAIL_CONCURRENCY", "4"))
DETAIL_LIMIT = int(os.getenv("DETAIL_LIMIT", "250"))
HEADLESS = os.getenv("HEADLESS", "1") != "0"

SOLES_RE = re.compile(r"S/\s*([0-9][0-9.,\s]*)", re.I)
MILES_RE = re.compile(r"(?<![0-9])([0-9][0-9.,\s]{0,18})\s*(?:millas?|miles)\s*(?:benefit)?", re.I)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean(value: Any) -> str | None:
    if value is None:
        return None
    value = re.sub(r"\s+", " ", str(value)).strip()
    return value or None


def parse_localized_number(raw: str, integer: bool = False) -> float | int | None:
    s = re.sub(r"[^0-9.,]", "", raw or "")
    if not s:
        return None
    if integer:
        d = re.sub(r"\D", "", s)
        return int(d) if d else None
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".") if len(s.rsplit(",", 1)[1]) == 2 else s.replace(",", "")
    elif "." in s and len(s.rsplit(".", 1)[1]) != 2:
        s = s.replace(".", "")
    try:
        n = float(s)
        return n if math.isfinite(n) else None
    except ValueError:
        return None


def extract_soles_values(text: str | None) -> list[float]:
    out = []
    for m in SOLES_RE.finditer(text or ""):
        n = parse_localized_number(m.group(1))
        if isinstance(n, (int, float)) and 0 < n < 10_000_000:
            out.append(round(float(n), 2))
    return sorted(set(out))


def extract_miles_values(text: str | None) -> list[int]:
    out = []
    for m in MILES_RE.finditer(text or ""):
        n = parse_localized_number(m.group(1), integer=True)
        if isinstance(n, int) and 0 < n < 100_000_000:
            out.append(n)
    return sorted(set(out))


def to_float(v: Any) -> float | None:
    try:
        n = float(v)
        return round(n, 2) if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def to_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def canonical_url(url: str | None) -> str | None:
    if not url:
        return None
    p = urlparse(urljoin(SOURCE_URL, url))
    path = re.sub(r"/+", "/", p.path).rstrip("/")
    return f"{p.scheme or 'https'}://{p.netloc or 'www.shopstar.pe'}{path}"


def url_key(url: str | None) -> str | None:
    url = canonical_url(url)
    return urlparse(url).path.lower().rstrip("/") if url else None


def product_nodes(obj: Any):
    if isinstance(obj, dict):
        if ("productName" in obj or "productId" in obj) and ("items" in obj or "link" in obj or "linkText" in obj):
            yield obj
        for v in obj.values():
            yield from product_nodes(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from product_nodes(v)


def best_offer(raw: dict[str, Any]) -> dict[str, Any]:
    offers = []
    for item in raw.get("items") or []:
        for seller in item.get("sellers") or []:
            o = seller.get("commertialOffer") or seller.get("commercialOffer") or {}
            price = o.get("Price")
            if isinstance(price, (int, float)) and price > 0:
                images = item.get("images") or []
                offers.append({
                    "sku_id": item.get("itemId"), "ean": item.get("ean"),
                    "seller_id": seller.get("sellerId"), "seller": seller.get("sellerName"),
                    "price": float(price), "list_price": to_float(o.get("ListPrice")),
                    "available_quantity": to_int(o.get("AvailableQuantity")),
                    "available": bool(o.get("IsAvailable", True)),
                    "image": next((x.get("imageUrl") for x in images if isinstance(x, dict) and x.get("imageUrl")), None)
                })
    available = [x for x in offers if x["available"] and (x["available_quantity"] or 0) > 0]
    pool = available or offers
    return min(pool, key=lambda x: x["price"]) if pool else {}


def normalize_network(raw: dict[str, Any]) -> dict[str, Any]:
    offer = best_offer(raw)
    link = raw.get("link") or (f"/{str(raw.get('linkText')).strip('/')}/p" if raw.get("linkText") else None)
    tags = []
    for field in ("clusterHighlights", "productClusters"):
        if isinstance(raw.get(field), dict):
            tags += [clean(x) for x in raw[field].values() if clean(x)]
    return {
        "product_id": str(raw.get("productId")) if raw.get("productId") is not None else None,
        "sku_id": str(offer.get("sku_id")) if offer.get("sku_id") is not None else None,
        "ean": clean(offer.get("ean")), "name": clean(raw.get("productName")), "brand": clean(raw.get("brand")),
        "seller": clean(offer.get("seller")), "seller_id": clean(offer.get("seller_id")),
        "categories": [clean(x) for x in (raw.get("categories") or []) if clean(x)],
        "campaign_tags": sorted(set(tags)), "url": canonical_url(link), "image": offer.get("image"),
        "price_soles": to_float(offer.get("price")), "list_price_soles": to_float(offer.get("list_price")),
        "available_quantity": to_int(offer.get("available_quantity")), "available": offer.get("available") if offer else None,
        "source_product": raw
    }


def from_dom(card: dict[str, Any]) -> dict[str, Any]:
    text = clean(card.get("text"))
    soles, miles = extract_soles_values(text), extract_miles_values(text)
    return {
        "product_id": None, "sku_id": None, "ean": None, "name": clean(card.get("title")), "brand": None,
        "seller": None, "seller_id": None, "categories": [], "campaign_tags": [],
        "url": canonical_url(card.get("url")), "image": card.get("image"),
        "price_soles": min(soles) if soles else None, "list_price_soles": max(soles) if len(soles) > 1 else None,
        "available_quantity": None, "available": None,
        "miles_benefit": min(miles) if miles else None, "miles_reference": max(miles) if len(miles) > 1 else None,
        "card_text": text, "source_product": None
    }


def merge_dom(product: dict[str, Any], card: dict[str, Any] | None) -> None:
    if not card:
        product.setdefault("card_text", None)
        return
    text = clean(card.get("text"))
    soles, miles = extract_soles_values(text), extract_miles_values(text)
    product["card_text"] = text
    product["image"] = product.get("image") or card.get("image")
    product["name"] = product.get("name") or clean(card.get("title"))
    if miles:
        product["miles_benefit"] = min(miles)
        product["miles_reference"] = max(miles) if len(miles) > 1 else None
    if product.get("price_soles") is None and soles:
        product["price_soles"] = min(soles)
    if product.get("list_price_soles") is None and len(soles) > 1:
        product["list_price_soles"] = max(soles)


def finalize(p: dict[str, Any]) -> None:
    price, miles = to_float(p.get("price_soles")), to_int(p.get("miles_benefit"))
    p["price_soles"], p["miles_benefit"] = price, miles
    if price and miles:
        ratio = price / miles
        p["soles_per_mile"] = round(ratio, 6)
        p["centimos_per_mile"] = round(ratio * 100, 4)
        p["soles_per_1000_miles"] = round(ratio * 1000, 2)
        p["miles_per_sol"] = round(miles / price, 4)
    else:
        p["soles_per_mile"] = p["centimos_per_mile"] = p["soles_per_1000_miles"] = p["miles_per_sol"] = None
    lp = to_float(p.get("list_price_soles"))
    p["cash_discount_pct"] = round((1 - price / lp) * 100, 2) if lp and price and lp > price else None


async def load_all(page) -> None:
    stable, previous = 0, -1
    for _ in range(70):
        clicked = False
        for pat in (r"mostrar más", r"ver más", r"cargar más", r"más productos"):
            try:
                b = page.get_by_role("button", name=re.compile(pat, re.I)).last
                if await b.is_visible(timeout=250):
                    await b.click(timeout=2500); clicked = True; await page.wait_for_timeout(1400); break
            except Exception:
                pass
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1100)
        count = await page.locator('a[href*="/p"]').count()
        stable = stable + 1 if count <= previous and not clicked else 0
        previous = max(previous, count)
        if stable >= 5:
            break


async def dom_cards(page) -> list[dict[str, Any]]:
    return await page.eval_on_selector_all('a[href*="/p"]', r'''(anchors) => {
      const out=[], seen=new Set();
      for (const a of anchors) {
        const href=(a.href||a.getAttribute('href')||'').split('#')[0].split('?')[0].replace(/\/$/,'');
        if (!href || !/\/p$/i.test(href) || seen.has(href)) continue; seen.add(href);
        let node=a, selected=a.parentElement||a;
        for(let i=0;i<9 && node && node.parentElement;i++){
          node=node.parentElement; const text=(node.innerText||'').trim(); const links=node.querySelectorAll('a[href*="/p"]').length;
          if(text.length>=20 && text.length<=4500) selected=node;
          if(/Vendido|Agregar|S\/|Millas?|Benefit/i.test(text) && links<=5){selected=node;if(/S\/|Millas?|Benefit/i.test(text))break;}
        }
        const img=selected.querySelector('img')||a.querySelector('img');
        out.push({url:href,title:(a.innerText||a.getAttribute('aria-label')||img?.alt||'').trim(),text:(selected.innerText||'').trim(),image:img?.currentSrc||img?.src||null});
      }
      return out;
    }''')


async def enrich_detail(context, product: dict[str, Any], sem: asyncio.Semaphore) -> None:
    if product.get("miles_benefit") or not product.get("url"):
        return
    async with sem:
        page = await context.new_page()
        try:
            await page.goto(product["url"], wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1800)
            body = clean(await page.locator("body").inner_text(timeout=10000)) or ""
            miles, soles = extract_miles_values(body), extract_soles_values(body)
            if miles:
                product["miles_benefit"] = min(miles)
                product["miles_reference"] = max(miles) if len(miles) > 1 else product.get("miles_reference")
            if product.get("price_soles") is None and soles:
                product["price_soles"] = min(soles)
            product["detail_text_excerpt"] = body[:1800] or None
        except Exception as exc:
            product["detail_error"] = (clean(exc) or type(exc).__name__)[:500]
        finally:
            await page.close()


async def scrape() -> dict[str, Any]:
    captured, tasks = [], []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(locale="es-PE", timezone_id="America/Lima", viewport={"width":1440,"height":1600}, user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36")
        page = await context.new_page()

        async def capture(response):
            u, ctype = response.url.lower(), (response.headers.get("content-type") or "").lower()
            if "json" not in ctype or not any(x in u for x in ("product_search", "/products/search", "intelligent-search")):
                return
            try:
                data = await response.json(); captured.extend(deepcopy(x) for x in product_nodes(data))
            except Exception:
                pass

        page.on("response", lambda r: tasks.append(asyncio.create_task(capture(r))))
        await page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(3500)
        await load_all(page); await page.wait_for_timeout(1200)
        cards = await dom_cards(page)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        net = {}
        for raw in captured:
            p = normalize_network(raw); k = url_key(p.get("url"))
            if k and k not in net: net[k] = p
        dom = {url_key(x.get("url")): x for x in cards if url_key(x.get("url"))}
        keys = list(dict.fromkeys([*dom.keys(), *net.keys()]))
        products = []
        for k in keys:
            if not k: continue
            if k in net:
                p = net[k]; merge_dom(p, dom.get(k))
            else:
                p = from_dom(dom[k])
            products.append(p)

        missing = [p for p in products if not p.get("miles_benefit")][:DETAIL_LIMIT]
        sem = asyncio.Semaphore(max(1, DETAIL_CONCURRENCY))
        await asyncio.gather(*(enrich_detail(context, p, sem) for p in missing))
        await context.close(); await browser.close()

    for p in products: finalize(p)
    products.sort(key=lambda p: (p.get("soles_per_mile") is not None, p.get("soles_per_mile") or -1), reverse=True)
    with_ratio = [p for p in products if p.get("soles_per_mile") is not None]
    return {"metadata": {
        "source_url": SOURCE_URL, "scraped_at": now_iso(), "product_count": len(products),
        "products_with_miles": sum(1 for p in products if p.get("miles_benefit")), "products_with_ratio": len(with_ratio),
        "network_products_captured": len(captured), "dom_product_cards": len(cards),
        "ratio_definition": "soles_per_mile = current cash price in PEN / displayed Benefit miles",
        "best_ratio_rule": "Higher soles_per_mile (or soles_per_1000_miles) means more PEN value extracted per Benefit mile.",
        "scraper_version": "1.0.0"}, "products": products}


def write_outputs(payload: dict[str, Any]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    compact = [{k:p.get(k) for k in ("product_id","name","url","price_soles","miles_benefit","soles_per_1000_miles","available")} for p in payload["products"]]
    history = []
    if HISTORY_PATH.exists():
        try:
            x = json.loads(HISTORY_PATH.read_text(encoding="utf-8")); history = x if isinstance(x, list) else []
        except Exception:
            pass
    history.append({"scraped_at": payload["metadata"]["scraped_at"], "products": compact})
    HISTORY_PATH.write_text(json.dumps(history[-MAX_HISTORY_RUNS:], ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    payload = asyncio.run(scrape())
    if payload["metadata"]["product_count"] == 0:
        raise SystemExit("No products were found; refusing to overwrite data/latest.json with an empty scrape.")
    write_outputs(payload)
    print(json.dumps(payload["metadata"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
