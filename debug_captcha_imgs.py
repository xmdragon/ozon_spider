import asyncio
from playwright.async_api import async_playwright
from slider_solver import _get_slider_info

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9223")
        context = browser.contexts[0]
        page = next((p for p in context.pages if 'ozon' in p.url), None)
        if not page:
            print("No ozon page")
            return
        print(f"URL: {page.url}")
        info = await _get_slider_info(page)
        if not info:
            print("No slider info")
            # Try navigating to homepage
            await page.goto('https://www.ozon.ru/', wait_until='domcontentloaded')
            await asyncio.sleep(3)
            info = await _get_slider_info(page)
        import json
        print(json.dumps({k: v for k, v in (info or {}).items() if k not in ('allImgs',)}, indent=2))
        if info:
            print(f"All imgs: {info.get('allImgs', [])}")

asyncio.run(main())
