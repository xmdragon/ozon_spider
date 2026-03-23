"""Capture fresh slider images from live Chrome and analyze."""
import asyncio
import io
import json
import logging
from PIL import Image, ImageDraw
from slider_solver import _get_slider_info, _download, _find_gap_by_template

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')

async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9223")
        context = browser.contexts[0]
        page = next((p for p in context.pages if 'ozon' in p.url), context.pages[-1])
        print(f"Page: {page.url}")
        print(f"Title: {await page.title()}")

        info = await _get_slider_info(page)
        if not info or not info.get('slider'):
            print("No slider — navigating to homepage")
            await page.goto('https://www.ozon.ru/', wait_until='domcontentloaded')
            await asyncio.sleep(4)
            info = await _get_slider_info(page)

        if not info or not info.get('slider'):
            print("No slider found")
            return

        print(json.dumps({k:v for k,v in info.items() if k not in ('allImgs',)}, indent=2))

        pz_src = info['puzzleSrc']
        bg_src = info.get('captchaBgSrc')
        if not bg_src:
            bg_src = next((i['src'] for i in info.get('allImgs',[]) if i.get('id')=='image'), None)

        pz_bytes = _download(pz_src)
        bg_bytes = _download(bg_src)
        if not pz_bytes or not bg_bytes:
            print("Download failed")
            return

        # Save
        with open('/tmp/live_bg.png','wb') as f: f.write(bg_bytes)
        with open('/tmp/live_pz.png','wb') as f: f.write(pz_bytes)

        bg_img = Image.open(io.BytesIO(bg_bytes))
        pz_img = Image.open(io.BytesIO(pz_bytes))
        print(f"bg: {bg_img.size}  pz: {pz_img.size}")

        # Screenshot to compare
        await page.screenshot(path='/tmp/live_screen.png')

        bg_rect = info.get('bgImageRect') or info.get('captchaRect')
        sx = bg_rect['w'] / bg_img.width
        bg_x = bg_rect['x']
        puzzle_x = info['puzzle']['x']
        puzzle_img_x = (puzzle_x - bg_x) / sx

        gap_img_x = _find_gap_by_template(bg_bytes, pz_bytes)
        drag_img = gap_img_x - puzzle_img_x
        drag_screen = drag_img * sx

        print(f"sx={sx:.3f} bg_x={bg_x}")
        print(f"puzzle_screen={puzzle_x:.1f} puzzle_img={puzzle_img_x:.1f}")
        print(f"gap_img={gap_img_x}")
        print(f"drag_img={drag_img:.1f}  drag_screen={drag_screen:.1f}px")

        # Annotate
        result = bg_img.copy().convert('RGB')
        draw = ImageDraw.Draw(result)
        draw.rectangle([int(gap_img_x),0,int(gap_img_x)+pz_img.width,pz_img.height], outline='red', width=2)
        draw.rectangle([int(puzzle_img_x),0,int(puzzle_img_x)+pz_img.width,pz_img.height], outline='blue', width=2)
        result.save('/tmp/live_analysis.png')
        print("Saved /tmp/live_analysis.png (red=gap target, blue=puzzle now)")
        if drag_screen > 5:
            print(f"ACTION: drag right {drag_screen:.1f}px")
        elif drag_screen < -5:
            print(f"PROBLEM: need to drag LEFT {-drag_screen:.1f}px — slider can't go left!")
        else:
            print(f"ACTION: tiny drag {drag_screen:.1f}px — puzzle already near gap")

asyncio.run(main())
