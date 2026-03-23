import asyncio
from playwright.async_api import async_playwright
from extractor import classify_page

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9223")
        context = browser.contexts[0]
        for p in context.pages:
            print(f"Page: {p.url} | title: {await p.title()}")
            state = await classify_page(p)
            print(f"  State: {state}")
            if 'ozon' in p.url:
                content = await p.content()
                print(f"  Content len: {len(content)}")
                # Print first 600 chars of body text
                text = await p.evaluate("document.body ? document.body.innerText.substring(0,600) : ''")
                print(f"  Body text: {text!r}")
                await p.screenshot(path='/tmp/ozon_now.png')
                print("  Screenshot: /tmp/ozon_now.png")

asyncio.run(main())
