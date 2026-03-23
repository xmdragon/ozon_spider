"""Simulate the full solve_slider logic offline."""
import io, json
from PIL import Image
from slider_solver import _find_gap_by_template, _download

with open('/tmp/slider_info.json') as f:
    info = json.load(f)

with open('/tmp/slider_bg.png','rb') as f: bg_bytes = f.read()
with open('/tmp/slider_puzzle.png','rb') as f: pz_bytes = f.read()

bg_img = Image.open(io.BytesIO(bg_bytes))
pz_img = Image.open(io.BytesIO(pz_bytes))

bg_rect = info.get('bgImageRect') or info.get('captchaRect', {})
bg_screen_w = bg_rect.get('w', bg_img.width)
bg_screen_x = bg_rect.get('x', 703.5)
sx = bg_screen_w / bg_img.width

puzzle = info.get('puzzle', {})
puzzle_screen_x = puzzle.get('x', bg_screen_x)
puzzle_img_x = (puzzle_screen_x - bg_screen_x) / sx

gap_rel_x = _find_gap_by_template(bg_bytes, pz_bytes)
drag_img = gap_rel_x - puzzle_img_x
drag_screen = drag_img * sx

slider = info['slider']
track_w = info['sliderContainer']['w']

print(f"sx={sx:.3f}  bg_screen_x={bg_screen_x}  puzzle_screen_x={puzzle_screen_x:.1f}")
print(f"puzzle_img_x={puzzle_img_x:.1f}  gap_img_x={gap_rel_x:.1f}  drag_img={drag_img:.1f}")
print(f"drag_screen={drag_screen:.1f}px  (track={track_w}px)")
print()
if 5 < drag_screen < track_w:
    print(f"OK: drag {drag_screen:.1f}px is within track [{5}, {track_w}]")
else:
    print(f"WARNING: drag {drag_screen:.1f}px out of range [5, {track_w}]")
