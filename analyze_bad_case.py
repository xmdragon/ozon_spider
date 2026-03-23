"""Download and analyze the bad case: gap_img=0 score=4.60"""
import io, urllib.request
import numpy as np
from PIL import Image, ImageDraw
from slider_solver import _find_gap_by_template

pz_url = 'https://cdn2.ozone.ru/s3/abt-challenge/cpt/5/3697d996.png'
bg_url = 'https://cdn2.ozone.ru/s3/abt-challenge/cpt/5/e2aa3b18.png'

def dl(url):
    req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read()

try:
    pz_bytes = dl(pz_url)
    bg_bytes = dl(bg_url)
except Exception as e:
    print(f"Download failed (images may have expired): {e}")
    # Try local files if available
    try:
        with open('/tmp/live_bg.png','rb') as f: bg_bytes=f.read()
        with open('/tmp/live_pz.png','rb') as f: pz_bytes=f.read()
        print("Using local files instead")
    except:
        print("No local files either, cannot analyze")
        exit(1)

bg_img = Image.open(io.BytesIO(bg_bytes)).convert('RGBA')
pz_img = Image.open(io.BytesIO(pz_bytes)).convert('RGBA')
print(f"bg: {bg_img.size}  pz: {pz_img.size}")

# Check puzzle alpha channel
pz_arr = np.array(pz_img)
pz_alpha = pz_arr[:,:,3]
print(f"Puzzle alpha: min={pz_alpha.min()} max={pz_alpha.max()} mean={pz_alpha.mean():.1f}")
print(f"Puzzle mask coverage (alpha>30): {(pz_alpha>30).mean()*100:.1f}%")

# Run template match and show top scores
bg = np.array(bg_img).astype(np.float32)
pz = np.array(pz_img).astype(np.float32)
bg_gray = np.mean(bg[:,:,:3], axis=2)
pz_gray = np.mean(pz[:,:,:3], axis=2)
pz_mask = pz_alpha > 30
ph, pw = pz_gray.shape
bh, bw = bg_gray.shape

scores = []
for x in range(0, bw-pw, 1):
    region = bg_gray[0:ph, x:x+pw]
    diff = np.abs(region - pz_gray)
    score = np.mean(diff[pz_mask]) if pz_mask.any() else np.mean(diff)
    scores.append((x, score))
scores.sort(key=lambda s: s[1])

print("\nTop 10 candidates:")
for x,s in scores[:10]:
    print(f"  x={x:3d} score={s:.2f}")

# Visualize all scores
print(f"\nScore range: {scores[0][1]:.2f} - {scores[-1][1]:.2f}")
best_x = scores[0][0]

# Also check: where is the gap visually in the bg image?
# The gap should appear as a dark region in the bg
bg_rgb = np.array(bg_img.convert('RGB')).astype(float)
col_mean = np.mean(bg_rgb[:ph,:,:], axis=(0,2))  # mean per column in puzzle row
print(f"\nColumn brightness in puzzle row range (first 20 cols): {col_mean[:20].astype(int).tolist()}")

# Save annotated
result = bg_img.convert('RGB').copy()
draw = ImageDraw.Draw(result)
draw.rectangle([best_x,0,best_x+pw,ph], outline='red', width=2)
result.save('/tmp/bad_case_analysis.png')
print(f"\nSaved /tmp/bad_case_analysis.png (best_x={best_x})")
pz_img.convert('RGB').save('/tmp/bad_case_pz.png')
print("Saved /tmp/bad_case_pz.png")
