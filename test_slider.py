"""Test slider solver on current open Chrome page."""
import asyncio
from playwright.async_api import async_playwright
from slider_solver import solve_slider
from extractor import classify_page

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9223")
        context = browser.contexts[0]
        page = next((p for p in context.pages if 'ozon' in p.url), None)
        if not page:
            print("No ozon page open")
            return
        print(f"Page: {page.url}")
        print(f"State: {await classify_page(page)}")
        result = await solve_slider(page)
        print(f"Solver result: {result}")
        await asyncio.sleep(3)
        print(f"State after: {await classify_page(page)}")
        print(f"URL after: {page.url}")

asyncio.run(main())
