"""
Offline drag simulation test.
Uses captured slider images to verify the full solve_slider logic
without touching the live site.
"""
import asyncio
import io
import json
import logging
from unittest.mock import AsyncMock, MagicMock
import numpy as np
from PIL import Image

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')

from slider_solver import (
    _find_gap_by_template, _find_gap_x_from_images,
    _get_slider_info, solve_slider
)

# Load captured data
with open('/tmp/slider_info.json') as f:
    info = json.load(f)
with open('/tmp/slider_bg.png', 'rb') as f:
    bg_bytes = f.read()
with open('/tmp/slider_puzzle.png', 'rb') as f:
    pz_bytes = f.read()

bg_img = Image.open(io.BytesIO(bg_bytes))
pz_img = Image.open(io.BytesIO(pz_bytes))

print("=== Offline Drag Calculation ===")
print(f"bg: {bg_img.size}  pz: {pz_img.size}")

bg_rect = info.get('bgImageRect') or info.get('captchaRect')
bg_screen_x = bg_rect['x']   # 703.5
bg_screen_w = bg_rect['w']   # 512
sx = bg_screen_w / bg_img.width  # 512/400 = 1.28

puzzle = info['puzzle']
puzzle_screen_x = puzzle['x']    # e.g. 768.78
puzzle_img_x = (puzzle_screen_x - bg_screen_x) / sx  # in image coords

gap_img_x = _find_gap_by_template(bg_bytes, pz_bytes)
drag_img = gap_img_x - puzzle_img_x
drag_screen = drag_img * sx

slider = info['slider']
track_w = info['sliderContainer']['w']  # 480

print(f"bg_screen: x={bg_screen_x} w={bg_screen_w} sx={sx:.3f}")
print(f"puzzle_screen_x={puzzle_screen_x:.1f} -> puzzle_img_x={puzzle_img_x:.1f}")
print(f"gap_img_x={gap_img_x}")
print(f"drag_img={drag_img:.1f}  drag_screen={drag_screen:.1f}px")
print(f"track_w={track_w}")

if 5 < drag_screen < track_w:
    print(f"PASS: drag {drag_screen:.1f}px in range [5, {track_w}]")
else:
    print(f"FAIL: drag {drag_screen:.1f}px out of range [5, {track_w}]")

# Simulate what solve_slider does with a mock page
print("\n=== Mock solve_slider test ===")

async def test_mock():
    import urllib.request

    # Patch _download to return local files
    import slider_solver
    original_download = slider_solver._download

    def mock_download(url):
        if 'puzzle' in url or url == info.get('puzzleSrc', ''):
            return pz_bytes
        return bg_bytes

    slider_solver._download = mock_download

    # Mock page
    page = MagicMock()
    page.query_selector = AsyncMock(return_value=MagicMock())  # slider present
    page.title = AsyncMock(return_value='Antibot Captcha')

    # Mock evaluate to return our captured info
    async def mock_evaluate(script):
        return info
    page.evaluate = mock_evaluate

    # Mock mouse
    moves = []
    async def mock_move(x, y):
        moves.append((x, y))
    async def mock_down(): pass
    async def mock_up(): pass
    page.mouse = MagicMock()
    page.mouse.move = AsyncMock(side_effect=mock_move)
    page.mouse.down = AsyncMock(side_effect=mock_down)
    page.mouse.up = AsyncMock(side_effect=mock_up)

    result = await solve_slider(page)
    print(f"solve_slider returned: {result}")
    if moves:
        start = moves[0]
        end = moves[-1]
        actual_drag = end[0] - start[0]
        print(f"Mouse: start={start[0]:.1f} end={end[0]:.1f} actual_drag={actual_drag:.1f}px")
        print(f"Expected drag: ~{drag_screen:.1f}px")
        if abs(actual_drag - drag_screen) < 15:
            print("PASS: drag distance matches expected")
        else:
            print(f"WARN: drag off by {abs(actual_drag - drag_screen):.1f}px")

    slider_solver._download = original_download

asyncio.run(test_mock())
