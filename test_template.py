"""Test template matching on current captcha images."""
import io
import urllib.request
import json
import asyncio
from playwright.async_api import async_playwright
from slider_solver import _get_slider_info, _find_gap_by_template, _download
from PIL import Image
import numpy as np

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
            print("No slider")
            return

        puzzle_src = info['puzzleSrc']
        bg_src = info['captchaBgSrc']
        if not bg_src:
            imgs = info.get('allImgs', [])
            bg_src = next((i['src'] for i in imgs if i['id'] == 'image'), None)
        print(f"puzzle: {puzzle_src}")
        print(f"bg:     {bg_src}")

        pz_bytes = _download(puzzle_src)
        bg_bytes = _download(bg_src)
        if not pz_bytes or not bg_bytes:
            print("Download failed")
            return

        # Save images for inspection
        with open('/tmp/captcha_bg.png','wb') as f: f.write(bg_bytes)
        with open('/tmp/captcha_pz.png','wb') as f: f.write(pz_bytes)

        bg_img = Image.open(io.BytesIO(bg_bytes))
        pz_img = Image.open(io.BytesIO(pz_bytes))
        print(f"bg size: {bg_img.size}, puzzle size: {pz_img.size}")

        gap_rel_x = _find_gap_by_template(bg_bytes, pz_bytes)
        print(f"gap_rel_x in image coords: {gap_rel_x}")

        # Compute actual drag delta
        captcha_rect = info['captchaRect']
        puzzle_rect = info['puzzle']
        captcha_x = captcha_rect['x']
        captcha_w = captcha_rect['w']
        scale = captcha_w / bg_img.width
        gap_abs_x = captcha_x + gap_rel_x * scale
        drag_delta = gap_abs_x - puzzle_rect['x']
        print(f"scale={scale:.3f} gap_abs_x={gap_abs_x:.1f} puzzle_x={puzzle_rect['x']:.1f} drag_delta={drag_delta:.1f}")

asyncio.run(main())
