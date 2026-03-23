import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9223")
        context = browser.contexts[0]
        page = next((p for p in context.pages if 'ozon' in p.url), None)
        url = page.url
        title = await page.title()
        title_lower = title.lower()
        print(f"URL: {url}")
        print(f"Title: {title!r}")
        print(f"Title lower checks:")
        for k in ["antibot", "checking", "challenge", "проверка", "подождите"]:
            if k in title_lower:
                print(f"  MATCH: {k!r} in title")
        content = await page.content()
        for k in ["cdn-cgi/challenge", "cf-browser-verification", "__cf_chl", "antibot"]:
            if k in content:
                idx = content.find(k)
                print(f"  MATCH: {k!r} in content at pos {idx}")
                print(f"    context: {content[max(0,idx-50):idx+100]!r}")

asyncio.run(main())
