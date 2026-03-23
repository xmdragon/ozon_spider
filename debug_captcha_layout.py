"""Analyze captcha layout: where puzzle sits relative to background image on screen."""
import asyncio
import io
import numpy as np
from PIL import Image, ImageDraw
from playwright.async_api import async_playwright
from slider_solver import _get_slider_info, _download, _find_gap_by_template

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9223")
        context = browser.contexts[0]
        page = next((p for p in context.pages if 'ozon' in p.url), None)
        if not page:
            print("No ozon page")
            return

        info = await _get_slider_info(page)
        if not info or not info.get('slider'):
            print("No slider currently visible")
            return

        # Screenshot full page
        png = await page.screenshot(full_page=False)
        screen = Image.open(io.BytesIO(png)).convert("RGB")
        print(f"Screenshot size: {screen.size}")
        screen.save('/tmp/captcha_screen.png')

        # Layout info
        slider = info['slider']
        slider_bg = info['sliderBg']
        slider_container = info['sliderContainer']
        puzzle = info['puzzle']
        captcha = info['captchaRect']

        print(f"captcha area:    x={captcha['x']:.1f} y={captcha['y']:.1f} w={captcha['w']:.1f} h={captcha['h']:.1f}")
        print(f"puzzle (screen): x={puzzle['x']:.1f} y={puzzle['y']:.1f} w={puzzle['w']:.1f} h={puzzle['h']:.1f}")
        print(f"slider handle:   x={slider['x']:.1f} y={slider['y']:.1f} w={slider['w']:.1f}")
        print(f"slider track:    x={slider_container['x']:.1f} w={slider_container['w']:.1f}")

        # The background image (#image) rect
        img_rect = await page.evaluate("""
            () => {
                const img = document.getElementById('image');
                if (!img) return null;
                const r = img.getBoundingClientRect();
                return {x: r.x, y: r.y, w: r.width, h: r.height};
            }
        """)
        print(f"bg image (screen): {img_rect}")

        # Download images
        puzzle_src = info['puzzleSrc']
        bg_src = info.get('captchaBgSrc')
        if not bg_src:
            imgs = info.get('allImgs', [])
            bg_src = next((i['src'] for i in imgs if i['id'] == 'image'), None)

        pz_bytes = _download(puzzle_src)
        bg_bytes = _download(bg_src) if bg_src else None

        if pz_bytes and bg_bytes:
            bg_img = Image.open(io.BytesIO(bg_bytes))
            pz_img = Image.open(io.BytesIO(pz_bytes))
            print(f"bg image natural: {bg_img.size}")
            print(f"puzzle natural:   {pz_img.size}")

            gap_x_img = _find_gap_by_template(bg_bytes, pz_bytes)
            print(f"gap x in image coords: {gap_x_img}")

            # Scale factors
            if img_rect:
                sx = img_rect['w'] / bg_img.width
                sy = img_rect['h'] / bg_img.height
            else:
                sx = captcha['w'] / bg_img.width
                sy = captcha['h'] / bg_img.height
            print(f"scale: sx={sx:.3f} sy={sy:.3f}")

            # Gap in screen coords
            if img_rect:
                gap_screen_x = img_rect['x'] + gap_x_img * sx
            else:
                gap_screen_x = captcha['x'] + gap_x_img * sx
            print(f"gap screen x: {gap_screen_x:.1f}")

            # Puzzle current screen x (left edge)
            puzzle_screen_x = puzzle['x']
            print(f"puzzle screen x (left): {puzzle_screen_x:.1f}")

            # Drag delta = gap_screen_x - puzzle_screen_x
            drag_by_screen = gap_screen_x - puzzle_screen_x
            print(f"drag delta (screen direct): {drag_by_screen:.1f}px")

            # Drag via slider track mapping
            track_w = slider_container['w'] if slider_container.get('w', 0) > 10 else 480
            max_travel_img = bg_img.width - pz_img.width
            scale_track = track_w / max_travel_img
            drag_by_track = gap_x_img * scale_track
            print(f"drag delta (track mapping): {drag_by_track:.1f}px  (track={track_w} max_img={max_travel_img})")

            # Annotate screenshot
            draw = ImageDraw.Draw(screen)
            if img_rect:
                gx = int(gap_screen_x)
                gy = int(img_rect['y'])
                gh = int(img_rect['h'])
                draw.rectangle([gx, gy, gx + int(pz_img.width*sx), gy + gh], outline='red', width=2)
            draw.rectangle([int(puzzle['x']), int(puzzle['y']),
                            int(puzzle['x']+puzzle['w']), int(puzzle['y']+puzzle['h'])],
                           outline='blue', width=2)
            screen.save('/tmp/captcha_annotated.png')
            print("Saved /tmp/captcha_annotated.png (red=target, blue=current puzzle)")

asyncio.run(main())
