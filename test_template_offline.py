"""Offline test using captured slider images and exact layout."""
import io
import json
import numpy as np
from PIL import Image, ImageDraw
from slider_solver import _find_gap_by_template

with open('/tmp/slider_info.json') as f:
    info = json.load(f)

with open('/tmp/slider_bg.png', 'rb') as f:
    bg_bytes = f.read()
with open('/tmp/slider_puzzle.png', 'rb') as f:
    pz_bytes = f.read()

bg_img = Image.open(io.BytesIO(bg_bytes))
pz_img = Image.open(io.BytesIO(pz_bytes))
print(f"bg natural: {bg_img.size}  pz natural: {pz_img.size}")

bg_rect = info['bgImageRect']   # {x:703.5, y:225, w:512, h:384}
puzzle_rect = info['puzzle']     # screen coords

sx = bg_rect['w'] / bg_img.width   # 512/400 = 1.28
print(f"sx = {sx:.4f}")

# Puzzle initial x in image coords
puzzle_img_x = (puzzle_rect['x'] - bg_rect['x']) / sx
print(f"puzzle img x = {puzzle_img_x:.1f}")

# Find gap
gap_img_x = _find_gap_by_template(bg_bytes, pz_bytes)
print(f"gap img x = {gap_img_x}")

# Drag in image coords
drag_img = gap_img_x - puzzle_img_x
print(f"drag img = {drag_img:.1f}")

# Drag in screen coords
drag_screen = drag_img * sx
print(f"drag screen = {drag_screen:.1f}px")

# Annotate bg image
result = bg_img.copy().convert('RGB')
draw = ImageDraw.Draw(result)
# Gap position
draw.rectangle([int(gap_img_x), 0, int(gap_img_x)+pz_img.width, pz_img.height], outline='red', width=2)
# Puzzle initial position
draw.rectangle([int(puzzle_img_x), 0, int(puzzle_img_x)+pz_img.width, pz_img.height], outline='blue', width=2)
result.save('/tmp/slider_analysis.png')
print("Saved /tmp/slider_analysis.png (red=gap, blue=puzzle start)")
