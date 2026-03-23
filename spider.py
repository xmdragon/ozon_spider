"""
Main spider: CDP connect, homepage warmup, product page scrape,
slider handling, stable-page detection.
"""
import asyncio
import json
import random
import logging
import time

from playwright.async_api import async_playwright
from extractor import (
    classify_page, extract_product, extract_variant_from_api,
    STATE_PRODUCT, STATE_SLIDER, STATE_CHALLENGE, STATE_BLOCKED,
)
from config import (
    CDP_PORT, PAGE_LOAD_TIMEOUT,
    STABLE_POLL_INTERVAL, STABLE_MAX_WAIT, SLIDER_TIMEOUT,
)
from slider_solver import solve_slider

log = logging.getLogger(__name__)

HOME_URL = "https://www.ozon.ru/"
PRODUCT_URL = "https://www.ozon.ru/product/{sku}/"


async def human_delay(lo=1.5, hi=4.0):
    await asyncio.sleep(random.uniform(lo, hi))


async def wait_stable(page, max_wait=STABLE_MAX_WAIT, interval=STABLE_POLL_INTERVAL) -> str:
    """
    Poll page state until it stabilises (same state twice in a row)
    or max_wait seconds elapse. Returns final state.
    """
    prev = None
    deadline = time.time() + max_wait
    while time.time() < deadline:
        await asyncio.sleep(interval)
        state = await classify_page(page)
        log.info(f"  page state: {state} | url: {page.url[:80]}")
        if state == prev and state not in ("unknown",):
            return state
        prev = state
        # If we've landed on a product page, no need to keep waiting
        if state == STATE_PRODUCT:
            return state
        if state == STATE_BLOCKED:
            return state
    return prev or "unknown"



async def setup_page(context):
    """Create a new page with ru-RU settings and stealth headers."""
    page = await context.new_page()
    await page.set_extra_http_headers({
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    })
    return page


async def handle_challenge(page, label="", max_attempts=2) -> str:
    """Handle challenge/slider on current page. Return final state."""
    for attempt in range(max_attempts):
        state = await classify_page(page)
        log.info(f"{label} challenge loop attempt {attempt+1}: {state}")
        if state == STATE_PRODUCT:
            return state
        if state == STATE_BLOCKED:
            return state
        if state == STATE_SLIDER:
            solved = await solve_slider(page)
            if not solved:
                await human_delay(2, 4)
        elif state in (STATE_CHALLENGE, "unknown"):
            # Just wait — may auto-resolve
            log.info(f"{label} waiting for challenge/unknown to auto-resolve...")
            await asyncio.sleep(STABLE_POLL_INTERVAL * 2)
        elif state == "home":
            # Homepage loaded cleanly — challenge resolved
            return "home"
        else:
            break
        state = await wait_stable(page, max_wait=20)
        if state in (STATE_PRODUCT, STATE_BLOCKED):
            return state
    return await classify_page(page)



async def fetch_product(page, sku: str) -> dict | None:
    """
    Navigate to product page, fetch Page1+Page2 API in parallel via page JS context.
    Returns extracted variant data or None on failure.
    """
    url = PRODUCT_URL.format(sku=sku)
    log.info(f"Fetching SKU {sku}: {url}")

    await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT * 1000,
                    referer=HOME_URL)
    await human_delay(2, 4)

    state = await wait_stable(page)
    log.info(f"Product page initial stable state: {state}")

    if state in (STATE_SLIDER, STATE_CHALLENGE):
        state = await handle_challenge(page, label=f"[product {sku}]", max_attempts=1)

    if state == STATE_BLOCKED:
        log.warning(f"SKU {sku}: page blocked")
        return None

    if state != STATE_PRODUCT:
        log.warning(f"SKU {sku}: unexpected final state '{state}'")
        return None

    # Fetch Page1 + Page2 in parallel via page JS context (real session, avoids 403)
    page1_url = f"/api/entrypoint-api.bx/page/json/v2?url=%2Fproduct%2F{sku}%2F"
    page2_url = f"/api/entrypoint-api.bx/page/json/v2?url=%2Fproduct%2F{sku}%2F%3Flayout_container%3DpdpPage2column%26layout_page_index%3D2"
    try:
        results = await page.evaluate(f"""
            async () => {{
                const [r1, r2] = await Promise.all([
                    fetch('{page1_url}', {{credentials: 'include'}}).then(r => r.ok ? r.json() : null).catch(() => null),
                    fetch('{page2_url}', {{credentials: 'include'}}).then(r => r.ok ? r.json() : null).catch(() => null),
                ]);
                return [r1, r2];
            }}
        """)
        page1_widget_states = (results[0] or {}).get('widgetStates', {}) if results else {}
        page2_widget_states = (results[1] or {}).get('widgetStates', {}) if results else {}
        log.info(f"[api] page1={len(page1_widget_states)} page2={len(page2_widget_states)} widgets")
    except Exception as e:
        log.debug(f"[api] fetch error: {e}")
        page1_widget_states, page2_widget_states = {}, {}

    # ── Build variant from API data ──
    if page1_widget_states or page2_widget_states:
        variant = extract_variant_from_api(page1_widget_states, page2_widget_states, sku)
        if variant.get('name') and variant.get('price'):
            log.info(f"SKU {sku} SUCCESS (API): {variant['name'][:60]} | price={variant['price']}")
            return variant
        log.warning(f"SKU {sku}: API extraction incomplete (p1={len(page1_widget_states)} p2={len(page2_widget_states)}), falling back to DOM")

    # Fallback: DOM extraction
    data = await extract_product(page)
    if not data.get("name") or not data.get("price"):
        log.warning(f"SKU {sku}: extracted data incomplete: {data}")
        return None

    log.info(f"SKU {sku} SUCCESS (DOM): {data.get('name', '')[:60]} | price={data.get('price')}")
    return data


async def save_cookies(context, path: str = "cookies.json"):
    """Save context cookies to file."""
    try:
        import json
        cookies = await context.cookies()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        log.info(f"Saved {len(cookies)} cookies to {path}")
    except Exception as e:
        log.warning(f"Failed to save cookies: {e}")


async def load_cookies(context, path: str = "cookies.json"):
    """Load cookies from file into context."""
    try:
        import json, os
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            cookies = json.load(f)
        await context.add_cookies(cookies)
        log.info(f"Loaded {len(cookies)} cookies from {path}")
    except Exception as e:
        log.warning(f"Failed to load cookies: {e}")


async def run_spider(skus: list, cdp_port: int) -> list:
    """
    Connect to existing Chrome via CDP, warmup, then scrape SKUs.
    Returns list of successfully extracted product dicts.
    """
    results = []
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        except Exception as e:
            raise RuntimeError(f"Cannot connect to Chrome CDP on port {cdp_port}: {e}")

        context = browser.contexts[0] if browser.contexts else await browser.new_context(
            locale="ru-RU",
            timezone_id="Europe/Moscow",
        )

        # Load saved cookies if available
        await load_cookies(context)

        page = await setup_page(context)

        # Skip homepage warmup on first pass — go direct to product pages
        # Homepage warmup burns profiles due to slider challenge loops
        for sku in skus:
            try:
                data = await fetch_product(page, sku)
                if data:
                    results.append(data)
                    await save_cookies(context)
            except Exception as e:
                log.warning(f"SKU {sku} exception: {e}")
                if "closed" in str(e).lower() or "target" in str(e).lower():
                    log.error("Browser/page closed — ending session")
                    break
            await human_delay(3, 6)

        try:
            await save_cookies(context)
        except Exception:
            pass
        try:
            await page.close()
        except Exception:
            pass
    return results
