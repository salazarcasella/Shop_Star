from __future__ import annotations

import asyncio
import json
import math
import os
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


async def unique_product_count(page: Page) -> int:
    return await page.eval_on_selector_all(
        'a[href*="/p"]',
        """(a) => new Set(a.map(x => (x.href || x.getAttribute('href') || '').split('?')[0].split('#')[0].replace(/\\/$/, '')).filter(x => /\\/p$/i.test(x))).size""",
    )


async def click_show_more(page: Page) -> bool:
    try:
        return bool(await page.evaluate(r"""() => {
          const norm=s=>(s||'').replace(/\s+/g,' ').trim().toLowerCase();
          const wanted=new Set(['mostrar más','mostrar mas','ver más','ver mas','cargar más','cargar mas','más productos','mas productos']);
          for(const el of document.querySelectorAll('button,a,[role="button"],div,span')){
            if(!wanted.has(norm(el.innerText||el.textContent))) continue;
            const r=el.getBoundingClientRect(), st=getComputedStyle(el);
            if(r.width<2||r.height<2||st.display==='none'||st.visibility==='hidden') continue;
            el.scrollIntoView({block:'center'}); el.click(); return true;
          }
          return false;
        }"""))
    except Exception:
        return False


async def load_full_catalog(page: Page) -> dict[str, Any]:
    best = await unique_product_count(page)
    trace = [best]
    clicks = stable = 0
    for _ in range(100):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(700)
        clicked = await click_show_more(page)
        if clicked:
            clicks += 1
            await page.wait_for_timeout(1800)
            try:
                await page.wait_for_load_state("networkidle", timeout=2500)
            except Exception:
                pass
        else:
            await page.wait_for_timeout(700)
        count = await unique_product_count(page)
        trace.append(count)
        if count > best:
            best, stable = count, 0
        elif not clicked:
            stable += 1
        if stable >= 6:
            break
    return {"unique_product_links": best, "show_more_clicks": clicks, "coverage_trace": trace}


async def extract_dom_cards(page: Page) -> list[dict[str, Any]]:
    return await page.eval_on_selector_all('a[href*="/p"]', r"""(anchors)=>{
      const out=[],seen=new Set();
      for(const a of anchors){
        let href=(a.href||a.getAttribute('href')||'').split('?')[0].split('#')[0].replace(/\/$/,'');
        if(!href||!/\/p$/i.test(href)||seen.has(href)) continue; seen.add(href);
        let node=a,selected=a.parentElement||a,best=-1;
        for(let i=0;i<10&&node&&node.parentElement;i++){
          node=node.parentElement;
          const text=(node.innerText||'').replace(/\s+/g,' ').trim();
          const links=new Set([...node.querySelectorAll('a[href*="/p"]')].map(x=>(x.href||'').split('?')[0].split('#')[0])).size;
          if(text.length<15||text.length>1800||links>2) continue;
          let score=(/Vendido por/i.test(text)?4:0)+(/Millas?|Benefit/i.test(text)?4:0)+(/S\//.test(text)?3:0)+(/Agregar|No disponible/i.test(text)?2:0)+(links===1?2:0);
          if(score>=best){selected=node;best=score} if(score>=10) break;
        }
        const img=selected.querySelector('img')||a.querySelector('img');
        const text=(selected.innerText||'').replace(/\s+/g,' ').trim();
        const anchor=(a.innerText||a.getAttribute('aria-label')||img?.alt||'').replace(/\s+/g,' ').trim();
        out.push({url:href,title:anchor||text.split(/Vendido por|\d[\d., ]*\s+Millas?|S\//i)[0].trim(),text:text.length<=1800?text:'',image:img?.currentSrc||img?.src||null});
      }
      return out;
    }""")


def _float(v: Any) -> float | None:
    try:
        n=float(v); return round(n,2) if math.isfinite(n) else None
    except (TypeError,ValueError):
        return None


def _walk(v: Any):
    if isinstance(v,dict):
        yield v
        for x in v.values(): yield from _walk(x)
    elif isinstance(v,list):
        for x in v: yield from _walk(x)


def jsonld_fields(documents: list[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    prices: list[float] = []
    highs: list[float] = []
    for doc in documents:
        for node in _walk(doc):
            t=node.get('@type'); types=t if isinstance(t,list) else [t]
            if 'Product' in types:
                out['name']=out.get('name') or clean_text(node.get('name'))
                brand=node.get('brand'); brand=brand.get('name') if isinstance(brand,dict) else brand
                out['brand']=out.get('brand') or clean_text(brand)
                out['sku_id']=out.get('sku_id') or clean_text(node.get('sku'))
                out['ean']=out.get('ean') or clean_text(node.get('gtin13') or node.get('gtin') or node.get('gtin14'))
                image=node.get('image'); image=next((x for x in image if isinstance(x,str)),None) if isinstance(image,list) else image
                out['image']=out.get('image') or clean_text(image)
            if 'Offer' in types or 'AggregateOffer' in types or 'UnitPriceSpecification' in types:
                for k in ('price','lowPrice'):
                    n=_float(node.get(k))
                    if n and n>0: prices.append(n)
                n=_float(node.get('highPrice'))
                if n and n>0: highs.append(n)
                av=clean_text(node.get('availability'))
                if av: out['available']='OutOfStock' not in av
    if prices: out['price_soles']=min(prices)
    if highs: out['list_price_soles']=max(highs)
    return out


async def enrich_detail(context: BrowserContext, product: dict[str, Any], sem: asyncio.Semaphore) -> None:
    if (product.get('miles_benefit') and product.get('price_soles')) or not product.get('url'):
        return
    async with sem:
        page=await context.new_page()
        try:
            await page.goto(product['url'],wait_until='domcontentloaded',timeout=50000)
            await page.wait_for_timeout(1300)
            docs=[]
            for raw in await page.locator('script[type="application/ld+json"]').all_text_contents():
                try: docs.append(json.loads(raw))
                except Exception: pass
            st=jsonld_fields(docs)
            for k in ('name','brand','sku_id','ean','image','available'):
                if not product.get(k) and st.get(k) is not None: product[k]=st[k]
            if product.get('price_soles') is None and st.get('price_soles'):
                product['price_soles']=st['price_soles']; product['price_source']='jsonld'
            if product.get('list_price_soles') is None and st.get('list_price_soles'):
                product['list_price_soles']=st['list_price_soles']
            body=clean_text(await page.locator('body').inner_text(timeout=10000)) or ''
            miles,soles=extract_miles_values(body),extract_soles_values(body)
            if not product.get('miles_benefit') and miles:
                product['miles_benefit']=min(miles); product['miles_reference']=max(miles) if len(miles)>1 else product.get('miles_reference'); product['miles_source']='product_detail'
            if product.get('price_soles') is None and soles:
                product['price_soles']=min(soles); product['price_source']='product_detail_text'
            if product.get('list_price_soles') is None and len(soles)>1: product['list_price_soles']=max(soles)
            product['detail_text_excerpt']=body[:1200] or None
            product['detail_enriched']=True
        except Exception as exc:
            product['detail_error']=(clean_text(exc) or type(exc).__name__)[:500]
        finally:
            await page.close()


async def scrape() -> dict[str, Any]:
    captured: list[dict[str, Any]]=[]; tasks=[]
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(headless=HEADLESS)
        context=await browser.new_context(locale='es-PE',timezone_id='America/Lima',viewport={'width':1440,'height':1600},user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36')
        page=await context.new_page()
        async def capture(response):
            u=response.url.lower(); c=(response.headers.get('content-type') or '').lower()
            if 'json' not in c or not any(x in u for x in ('product_search','/products/search','intelligent-search')): return
            try:
                data=await response.json(); captured.extend(deepcopy(x) for x in iter_product_nodes(data))
            except Exception: pass
        page.on('response',lambda r: tasks.append(asyncio.create_task(capture(r))))
        await page.goto(SOURCE_URL,wait_until='domcontentloaded',timeout=90000); await page.wait_for_timeout(3500)
        coverage=await load_full_catalog(page); await page.wait_for_timeout(1000)
        cards=await extract_dom_cards(page)
        if tasks: await asyncio.gather(*tasks,return_exceptions=True)
        net={}
        for raw in captured:
            p=normalize_network_product(raw); k=product_path_key(p.get('url'))
            if k and k not in net: net[k]=p
        dom={product_path_key(x.get('url')):x for x in cards if product_path_key(x.get('url'))}
        products=[]
        for k in dict.fromkeys([*dom.keys(),*net.keys()]):
            if not k: continue
            if k in net:
                p=net[k]; merge_dom_data(p,dom.get(k))
            else: p=build_dom_only_product(dom[k])
            products.append(p)
        missing=[p for p in products if not (p.get('miles_benefit') and p.get('price_soles'))][:DETAIL_LIMIT]
        sem=asyncio.Semaphore(max(1,DETAIL_CONCURRENCY))
        await asyncio.gather(*(enrich_detail(context,p,sem) for p in missing))
        await context.close(); await browser.close()
    for p in products: finalize_product(p)
    products.sort(key=lambda p:(p.get('soles_per_mile') is not None,p.get('soles_per_mile') or -1),reverse=True)
    meta={
        'source_url':SOURCE_URL,'scraped_at':utc_now_iso(),'product_count':len(products),
        'products_with_miles':sum(1 for p in products if p.get('miles_benefit')),
        'products_with_ratio':sum(1 for p in products if p.get('soles_per_mile') is not None),
        'network_products_captured':len(captured),'dom_product_cards':len(cards),
        **coverage,'detail_pages_requested':len(missing),
        'detail_pages_enriched':sum(1 for p in products if p.get('detail_enriched')),
        'detail_errors':sum(1 for p in products if p.get('detail_error')),
        'ratio_definition':'soles_per_mile = current cash price in PEN / displayed Benefit miles',
        'best_ratio_rule':'Higher soles_per_mile (or soles_per_1000_miles) means more PEN value extracted per Benefit mile.',
        'scraper_version':'2.0.1'
    }
    return {'metadata':meta,'products':products}


def main() -> None:
    payload=asyncio.run(scrape()); count=payload['metadata']['product_count']
    if count < MIN_EXPECTED_PRODUCTS:
        raise SystemExit(f'Coverage check failed: found {count} products but expected at least {MIN_EXPECTED_PRODUCTS}; existing JSON remains untouched.')
    write_outputs(payload)
    print(json.dumps(payload['metadata'],ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
