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
from browser_pages import ensure_single_page
from extractor import (
    attach_page_observers,
    classify_page, extract_product, extract_variant_from_api,
    STATE_PRODUCT, STATE_ADULT, STATE_SLIDER, STATE_CHALLENGE, STATE_BLOCKED,
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
        state = await classify_page(page)
        log.info(f"  page state: {state} | url: {page.url[:80]}")
        if state == prev and state not in ("unknown",):
            return state
        prev = state
        # If we've landed on a product page, no need to keep waiting
        if state == STATE_PRODUCT:
            return state
        await asyncio.sleep(min(interval, 1.0))
    return prev or "unknown"


async def setup_page(context):
    """Prepare a page and attach observers.

    Do not override navigation-only headers here. Playwright applies these
    headers to all requests from the page, including cross-origin static
    assets, which breaks Ozon's CORS flow for ozonstatic resources.
    """
    page = await ensure_single_page(context.pages, context.new_page)
    attach_page_observers(page)
    return page


async def _dismiss_cookie_banner(page) -> None:
    try:
        ok_btn = page.locator('button:has-text("OK")').first
        if await ok_btn.count() > 0 and await ok_btn.is_visible():
            await ok_btn.click()
            await asyncio.sleep(0.3)
    except Exception:
        pass


async def _detect_buybox_currency(page) -> str | None:
    try:
        candidates = await page.evaluate(
            """
            () => {
                const items = [];
                const els = Array.from(document.querySelectorAll('div, span, a, button'));
                for (const el of els) {
                    const text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (!text || (!text.includes('¥') && !text.includes('₽'))) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (r.x < window.innerWidth * 0.5) continue;
                    if (r.y < 0 || r.y > window.innerHeight * 0.85) continue;
                    items.push({
                        text: text.slice(0, 160),
                        x: Math.round(r.x),
                        y: Math.round(r.y),
                        w: Math.round(r.width),
                        h: Math.round(r.height),
                    });
                }
                items.sort((a, b) => a.y - b.y || a.x - b.x);
                return items;
            }
            """
        )
        for item in candidates or []:
            text = str(item.get("text") or "")
            if "¥" in text:
                log.info("buybox currency detected as CNY from text: %s", text)
                return "CNY"
        for item in candidates or []:
            text = str(item.get("text") or "")
            if "₽" in text:
                log.info("buybox currency detected as RUB from text: %s", text)
                return "RUB"
    except Exception as e:
        log.info("detect buybox currency failed: %s", e)
    return None


async def _open_language_currency_modal(page) -> bool:
    try:
        await _dismiss_cookie_banner(page)
        ru_btn = page.locator('button:has-text("RU")').first
        if await ru_btn.count() == 0:
            return False
        await ru_btn.click(timeout=5000)
        await page.locator('text=Язык и валюта').wait_for(timeout=5000)
        await asyncio.sleep(0.5)
        return True
    except Exception as e:
        log.info("open language/currency modal failed: %s", e)
        return False


async def _close_language_currency_modal(page) -> None:
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.2)
    except Exception:
        pass


async def ensure_currency_cny(page) -> bool:
    """
    Ensure the product page is displayed in CNY before extracting prices.
    Returns True when the page currency was changed and a refresh is expected.
    """
    await _dismiss_cookie_banner(page)
    current_currency = await _detect_buybox_currency(page)
    if current_currency == "CNY":
        return False

    if not await _open_language_currency_modal(page):
        return False

    try:
        currency_box = page.get_by_role("combobox").last
        current_currency = ((await currency_box.text_content()) or "").strip()
    except Exception as e:
        log.info("read currency combobox failed: %s", e)
        await _close_language_currency_modal(page)
        return False

    if "cny" in current_currency.casefold() or "китайский юань" in current_currency.casefold():
        log.info("currency already CNY: %s", current_currency)
        await _close_language_currency_modal(page)
        return False

    try:
        await currency_box.click(timeout=5000)
        await asyncio.sleep(0.5)
        await page.locator('text=Китайский юань').first.click(timeout=5000)
        await asyncio.sleep(0.3)
        await page.locator('button:has-text("Сохранить")').click(timeout=5000)
        log.info("currency changed to CNY, waiting for page refresh")
        await page.wait_for_load_state("domcontentloaded", timeout=60000)
        await asyncio.sleep(3)
        return True
    except Exception as e:
        log.warning("set currency CNY failed: %s", e)
        await _close_language_currency_modal(page)
        return False


def _strip_empty_optional_fields(data: dict) -> dict:
    if not isinstance(data, dict):
        return data
    if data.get("specifications", None) == "":
        data.pop("specifications", None)
    return data


async def handle_challenge(page, label="", max_attempts=6) -> str:
    """Handle challenge/slider on current page. Return final state."""
    clean_url = page.url.split("?", 1)[0] if "/product/" in page.url else page.url
    for attempt in range(max_attempts):
        state = await classify_page(page)
        log.info(f"{label} challenge loop attempt {attempt+1}: {state}")
        if state == STATE_PRODUCT:
            return state
        if state == STATE_SLIDER:
            solved = await solve_slider(page)
            if solved:
                retried_clean_url = False
                deadline = time.time() + 30
                while time.time() < deadline:
                    state = await classify_page(page)
                    log.info(f"{label} post-slider state: {state} | url: {page.url[:120]}")
                    if state == STATE_PRODUCT:
                        return state
                    if state == "home":
                        return state
                    if state == STATE_ADULT:
                        return state
                    # After a successful drag, challenge pages often transition through
                    # temporary abt/rr states; do not treat them as terminal too early.
                    if state in (STATE_CHALLENGE, "unknown", STATE_BLOCKED):
                        if (
                            not retried_clean_url
                            and clean_url
                            and page.url != clean_url
                            and "/product/" in clean_url
                        ):
                            retried_clean_url = True
                            log.info("%s slider solved but page still transitional; retry clean product URL: %s", label, clean_url)
                            try:
                                await page.goto(clean_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT * 1000, referer=HOME_URL)
                            except Exception as e:
                                log.info("%s clean URL retry failed: %s", label, e)
                        try:
                            await page.wait_for_load_state("domcontentloaded", timeout=500)
                        except Exception:
                            pass
                        await asyncio.sleep(0.5)
                        continue
                    break
            else:
                await human_delay(2, 4)
        elif state in (STATE_CHALLENGE, STATE_BLOCKED, "unknown"):
            # antibot/403 pages may be transient and can auto-resolve back to product
            log.info(f"{label} waiting for challenge/blocked/unknown to auto-resolve...")
            await asyncio.sleep(STABLE_POLL_INTERVAL * 2)
        elif state == "home":
            # Homepage loaded cleanly — challenge resolved
            return "home"
        else:
            break
        state = await wait_stable(page, max_wait=20)
        if state == STATE_PRODUCT:
            return state
    return await classify_page(page)


async def eval_in_main_world(page, expression: str):
    """Run JavaScript in the page's main world via CDP, similar to DevTools console."""
    session = await page.context.new_cdp_session(page)
    try:
        result = await session.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        if "exceptionDetails" in result:
            raise RuntimeError(str(result["exceptionDetails"]))
        return (result.get("result") or {}).get("value")
    finally:
        try:
            await session.detach()
        except Exception:
            pass


async def handle_adult_prompt(page, sku: str, label="") -> str:
    """Handle adult-confirm modal/page by polling until modal disappears or timeout."""
    await asyncio.sleep(0.08)
    deadline = time.time() + 18
    attempt = 0

    while time.time() < deadline:
        attempt += 1
        state = await classify_page(page)
        log.info(f"{label} adult loop attempt {attempt}: {state}")
        if state == STATE_PRODUCT:
            return state
        if state != STATE_ADULT:
            return state

        modal = page.locator('[data-widget="userAdultModal"]').first
        if await modal.count() == 0:
            return await classify_page(page)

        modal_text = " ".join((await modal.text_content() or "").split())
        needs_birthdate = (
            "Пожалуйста, укажите дату вашего рождения" in modal_text
            or "дату вашего рождения" in modal_text
        )

        if needs_birthdate:
            try:
                await eval_in_main_world(
                    page,
                    """(() => {
                        const modal = document.querySelector('[data-widget="userAdultModal"]');
                        const outer = modal.querySelector('div[type="text"][name="birthdate"]');
                        const input = modal.querySelector('input');
                        const targetValue = '01.01.2000';
                        if (outer) {
                          outer.setAttribute('value', targetValue);
                          outer.dispatchEvent(new Event('input', { bubbles: true }));
                          outer.dispatchEvent(new Event('change', { bubbles: true }));
                          outer.dispatchEvent(new Event('blur', { bubbles: true }));
                        }
                        if (input) {
                          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
                          if (setter) setter.call(input, targetValue);
                          else input.value = targetValue;
                          for (const evt of ['input', 'change', 'blur', 'keyup']) {
                            input.dispatchEvent(new Event(evt, { bubbles: true }));
                          }
                        }
                    })()"""
                )
                await asyncio.sleep(0.35)
            except Exception as e:
                log.info("%s adult birthdate sequence failed: %s", label, e)

        result = await eval_in_main_world(
            page,
            """(() => {
                const modal = document.querySelector('[data-widget="userAdultModal"]');
                if (!modal) {
                    return { modalExists: false };
                }

                const buttons = Array.from(modal.querySelectorAll('button'));
                const getText = (node) => String(node?.textContent || '').replace(/\\s+/g, ' ').trim();
                const confirmBtn = buttons.find((btn) => getText(btn).includes('Подтвердить'));
                const input = modal.querySelector('input');
                const outerField = modal.querySelector('div[type="text"][name="birthdate"]');
                const inner = confirmBtn ? confirmBtn.querySelector('.b25_7_0-a') : null;

                return {
                    modalExists: true,
                    outerValue: outerField ? outerField.getAttribute('value') : null,
                    inputValue: input ? input.value : null,
                    confirmDisabled: confirmBtn ? !!confirmBtn.disabled : null,
                    hasDisabledAttr: confirmBtn ? confirmBtn.hasAttribute('disabled') : null,
                    canClick: !!confirmBtn && !confirmBtn.disabled,
                    innerBg: inner ? getComputedStyle(inner).backgroundColor : null,
                };
            })()"""
        )

        log.info("%s adult modal state: %s", label, result)

        confirm_btn = modal.locator("button", has_text="Подтвердить").first
        clicked = False
        if await confirm_btn.count() > 0:
            for _ in range(10):
                try:
                    if not await confirm_btn.is_disabled():
                        await confirm_btn.click()
                        clicked = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.15)

        if clicked:
            log.info("%s adult confirm clicked", label)

        ready_result = await page.evaluate(
            """(sku) => {
                const normalize = (text) => String(text || '').replace(/\\s+/g, ' ').trim();
                const href = String(location.href || '');
                const title = normalize(document.title || '').toLowerCase();
                const bodyText = normalize(
                    document?.body?.innerText
                    || document?.documentElement?.innerText
                    || ''
                );
                const modal = document.querySelector('[data-widget="userAdultModal"]');
                const looksLikeAdultPrompt =
                    !!modal
                    || title.includes('подтвердите возраст')
                    || bodyText.includes('Подтвердите возраст')
                    || bodyText.includes('дату вашего рождения');
                const hasProductContent =
                    !!document.querySelector('[data-widget="webProductHeading"]')
                    || !!document.querySelector('[data-widget="webGallery"]')
                    || !!document.querySelector('[data-widget="pdpGallery"]')
                    || !!document.querySelector('[data-widget="stickyContainer"]');
                const hasBuyAction = Array.from(document.querySelectorAll('button, a'))
                    .map((node) => normalize(node.textContent || ''))
                    .some((text) =>
                        text.includes('В корзину')
                        || text.includes('Купить')
                        || text.includes('Купить сейчас')
                    );
                const isTargetProductPage =
                    href.includes('/product/')
                    && (
                        href.includes('/' + sku + '/')
                        || href.includes('-' + sku + '/')
                        || href.includes('/' + sku + '?')
                    );
                const looksLikeReadyProductPage =
                    isTargetProductPage
                    && !looksLikeAdultPrompt
                    && !title.includes('antibot')
                    && (hasProductContent || hasBuyAction || bodyText.length > 40);
                return { done: !modal && looksLikeReadyProductPage };
            }""",
            sku,
        )
        if ready_result.get("done"):
            log.info("%s adult confirm finished via ready_dom", label)
            return STATE_PRODUCT

        try:
            api_result = await page.evaluate(
                """async (sku) => {
                    try {
                        const resp = await fetch(
                            '/api/entrypoint-api.bx/page/json/v2?url=' + encodeURIComponent('/product/' + sku),
                            { credentials: 'include' }
                        );
                        const rawText = await resp.text();
                        if (String(rawText || '').trimStart().startsWith('<')) {
                            return { html: true, modalGone: false };
                        }
                        const json = JSON.parse(rawText);
                        const ws = json.widgetStates || {};
                        const modalGone = !Object.keys(ws).some((k) => k.startsWith('userAdultModal'));
                        return { html: false, modalGone };
                    } catch (e) {
                        return { error: String(e) };
                    }
                }""",
                sku,
            )
            if api_result.get("modalGone"):
                log.info("%s adult confirm finished via api modalGone", label)
                return STATE_PRODUCT
        except Exception as e:
            log.debug("%s adult api probe failed: %s", label, e)

        await asyncio.sleep(0.12)

    return await classify_page(page)



async def fetch_product(page, sku: str) -> tuple[dict | None, str]:
    """
    Navigate to product page, fetch Page1+Page2 API in parallel via page JS context.
    Returns (data, status), where status is one of:
    - ok
    - blocked
    - unavailable
    """
    url = PRODUCT_URL.format(sku=sku)
    log.info(f"Fetching SKU {sku}: {url}")

    await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT * 1000,
                    referer=HOME_URL)
    await human_delay(0.4, 0.9)

    state = await wait_stable(page)
    log.info(f"Product page initial stable state: {state}")

    if state == STATE_ADULT:
        state = await handle_adult_prompt(page, sku, label=f"[product {sku}]")

    if state in (STATE_SLIDER, STATE_CHALLENGE, STATE_BLOCKED):
        state = await handle_challenge(page, label=f"[product {sku}]", max_attempts=6)

    if state == STATE_ADULT:
        state = await handle_adult_prompt(page, sku, label=f"[product {sku}]")

    if state == STATE_BLOCKED:
        log.warning(f"SKU {sku}: page blocked")
        return None, "blocked"

    if state != STATE_PRODUCT:
        log.warning(f"SKU {sku}: unexpected final state '{state}'")
        return None, "unavailable"

    currency_changed = await ensure_currency_cny(page)
    if currency_changed:
        state = await wait_stable(page)
        log.info(f"Product page state after currency change: {state}")
        if state == STATE_ADULT:
            state = await handle_adult_prompt(page, sku, label=f"[product {sku}]")
        if state in (STATE_SLIDER, STATE_CHALLENGE, STATE_BLOCKED):
            state = await handle_challenge(page, label=f"[product {sku}][currency]", max_attempts=6)
        if state == STATE_ADULT:
            state = await handle_adult_prompt(page, sku, label=f"[product {sku}]")
        if state == STATE_BLOCKED:
            log.warning(f"SKU {sku}: page blocked after currency change")
            return None, "blocked"
        if state != STATE_PRODUCT:
            log.warning(f"SKU {sku}: unexpected state after currency change '{state}'")
            return None, "unavailable"

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
        if not variant.get("link"):
            try:
                variant["link"] = await page.evaluate(
                    """() => {
                        const canonical = document.querySelector('link[rel="canonical"]')?.getAttribute('href') || '';
                        const raw = canonical || location.pathname || '';
                        if (!raw) return '';
                        try {
                            const u = new URL(raw, location.origin);
                            return u.pathname;
                        } catch {
                            return String(raw).split('?')[0];
                        }
                    }"""
                )
            except Exception:
                pass
        if variant.get('name') and variant.get('price'):
            log.info(f"SKU {sku} SUCCESS (API): {variant['name'][:60]} | price={variant['price']}")
            variant = _strip_empty_optional_fields(variant)
            return variant, "ok"
        log.warning(f"SKU {sku}: API extraction incomplete (p1={len(page1_widget_states)} p2={len(page2_widget_states)}), falling back to DOM")

    # Fallback: DOM extraction
    data = await extract_product(page)
    if not data.get("name") or not data.get("price"):
        log.warning(f"SKU {sku}: extracted data incomplete: {data}")
        return None, "unavailable"

    log.info(f"SKU {sku} SUCCESS (DOM): {data.get('name', '')[:60]} | price={data.get('price')}")
    data = _strip_empty_optional_fields(data)
    return data, "ok"


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
                data, status = await fetch_product(page, sku)
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
