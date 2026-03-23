"""Inspect slider dimensions to calibrate drag distance."""
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9223")
        context = browser.contexts[0]
        page = next((p for p in context.pages if 'ozon' in p.url), context.pages[-1])
        print(f"URL: {page.url}")

        info = await page.evaluate("""
            () => {
                const slider = document.getElementById('slider');
                const bg = document.getElementById('slider-background');
                const container = document.getElementById('slider-container');
                const puzzle = document.getElementById('puzzle');
                const captcha = document.getElementById('captcha');
                const toRect = el => el ? JSON.parse(JSON.stringify(el.getBoundingClientRect())) : null;
                return {
                    slider: toRect(slider),
                    sliderStyle: slider ? slider.getAttribute('style') : null,
                    sliderBg: toRect(bg),
                    sliderContainer: toRect(container),
                    puzzle: toRect(puzzle),
                    puzzleSrc: puzzle ? puzzle.src : null,
                    captcha: toRect(captcha),
                    captchaData: document.querySelector('.captcha-data') ?
                        document.querySelector('.captcha-data').innerHTML.substring(0,500) : null
                };
            }
        """)
        import json
        print(json.dumps(info, indent=2))

asyncio.run(main())
