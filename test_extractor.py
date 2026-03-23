"""Test updated extractor on current product page."""
import asyncio
from playwright.async_api import async_playwright
from extractor import extract_product, classify_page

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9223")
        context = browser.contexts[0]
        # Use any ozon page
        page = next((p for p in context.pages if 'ozon' in p.url and p.url != 'about:blank'), None)
        if not page:
            print("No ozon page")
            return
        print(f"URL: {page.url}")
        state = await classify_page(page)
        print(f"State: {state}")
        data = await extract_product(page)
        print(f"Data: {data}")

asyncio.run(main())
