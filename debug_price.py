"""Debug price extraction on current product pages."""
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9223")
        context = browser.contexts[0]
        page = next((p for p in context.pages if '/product/' in p.url), None)
        if not page:
            print("No product page")
            return
        print(f"URL: {page.url}")

        # Try all price-related selectors
        price_selectors = [
            "[data-widget='webPrice'] [class*='price']",
            "[class*='price-number']",
            "[class*='price_number']",
            "span[class*='Price']",
            "[class*='price']",
        ]
        for sel in price_selectors:
            els = await page.query_selector_all(sel)
            if els:
                for el in els[:3]:
                    txt = (await el.inner_text()).strip()
                    if txt and any(c.isdigit() for c in txt):
                        print(f"  sel={sel!r} -> {txt!r}")
                        break

        # Broader search
        result = await page.evaluate("""
            () => {
                // Find elements containing ₽ with digits
                const results = [];
                document.querySelectorAll('*').forEach(el => {
                    const txt = el.innerText || '';
                    if (txt.includes('\u20bd') && /\\d/.test(txt) && el.children.length === 0) {
                        const cls = typeof el.className === 'string' ? el.className : '';
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) {
                            results.push({tag: el.tagName, cls: cls.substring(0,80),
                                         txt: txt.substring(0,30),
                                         w: Math.round(r.width), h: Math.round(r.height)});
                        }
                    }
                });
                return results.slice(0, 15);
            }
        """)
        print(f"\nElements containing ₽:")
        for el in result:
            print(f"  {el['tag']} cls={el['cls']!r} txt={el['txt']!r}")

asyncio.run(main())
