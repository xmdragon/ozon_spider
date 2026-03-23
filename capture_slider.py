"""Wait for slider captcha and save all images + layout info for offline analysis."""
import asyncio
import json
import urllib.request
from playwright.async_api import async_playwright
from slider_solver import _get_slider_info

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9223")
        context = browser.contexts[0]
        page = next((p for p in context.pages if 'ozon' in p.url), context.pages[-1])
        print(f"Current: {page.url}")

        # Navigate to homepage to trigger slider
        await page.goto('https://www.ozon.ru/', wait_until='domcontentloaded')
        await asyncio.sleep(4)

        info = await _get_slider_info(page)
        if not info or not info.get('slider'):
            print("No slider — page may be clean")
            print(f"Title: {await page.title()}")
            return

        print("Slider found! Capturing layout...")
        print(json.dumps({k:v for k,v in info.items() if k not in ('allImgs',)}, indent=2))
        print(f"allImgs: {info.get('allImgs')}")

        # Screenshot
        await page.screenshot(path='/tmp/slider_capture.png')
        print("Screenshot: /tmp/slider_capture.png")

        # Download images
        def dl(url, path):
            try:
                req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = r.read()
                with open(path,'wb') as f: f.write(data)
                print(f"Saved {path} ({len(data)} bytes)")
            except Exception as e:
                print(f"Failed {url}: {e}")

        if info.get('puzzleSrc'):
            dl(info['puzzleSrc'], '/tmp/slider_puzzle.png')
        bg_src = info.get('captchaBgSrc')
        if not bg_src:
            for img in info.get('allImgs', []):
                if img.get('id') == 'image':
                    bg_src = img['src']
                    break
        if bg_src:
            dl(bg_src, '/tmp/slider_bg.png')

        # Also save full info as JSON
        with open('/tmp/slider_info.json', 'w') as f:
            json.dump(info, f, indent=2)
        print("Info: /tmp/slider_info.json")

asyncio.run(main())
