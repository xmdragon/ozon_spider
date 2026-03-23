"""
Ozon slider captcha solver.
Downloads the captcha background + puzzle images and uses template matching
to find the exact gap position.
"""
import asyncio
import random
import logging
import io
import urllib.request

log = logging.getLogger(__name__)


async def _get_slider_info(page) -> dict | None:
    return await page.evaluate("""
        () => {
            const slider = document.getElementById('slider');
            const bg = document.getElementById('slider-background');
            const container = document.getElementById('slider-container');
            const puzzle = document.getElementById('puzzle');
            const captcha = document.getElementById('captcha');
            const bgImg = document.getElementById('bg') || document.getElementById('background');
            if (!slider || !bg) return null;
            const toR = el => {
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return {x: r.x, y: r.y, w: r.width, h: r.height};
            };
            // Try to get background image src from captcha div style
            let captchaBgSrc = null;
            if (captcha) {
                const style = window.getComputedStyle(captcha);
                const bgImg = style.backgroundImage;
                if (bgImg && bgImg !== 'none') {
                    captchaBgSrc = bgImg.replace(/url[(]["']{0,1}/, '').replace(/["']{0,1}[)]$/, '');
                }
                // Also check img children
                const imgs = captcha.querySelectorAll('img');
                if (!captchaBgSrc && imgs.length > 0) {
                    captchaBgSrc = imgs[0].src;
                }
            }
            // Get all images in the captcha container
            const allImgs = [];
            document.querySelectorAll('#captcha-container img, #captcha img').forEach(img => {
                allImgs.push({src: img.src, id: img.id, cls: img.className,
                              w: img.naturalWidth, h: img.naturalHeight});
            });
            const bgImageEl = document.getElementById('image');
            return {
                slider: toR(slider),
                sliderBg: toR(bg),
                sliderContainer: toR(container),
                puzzle: toR(puzzle),
                puzzleSrc: puzzle ? puzzle.src : null,
                captchaRect: toR(captcha),
                bgImageRect: toR(bgImageEl),
                captchaBgSrc,
                allImgs,
            };
        }
    """)


def _find_gap_by_template(bg_bytes: bytes, puzzle_bytes: bytes) -> float | None:
    """
    Find where the puzzle piece fits in the background.
    Uses edge-map cross-correlation for sub-pixel accuracy.
    Returns the x offset (0-based, relative to captcha bg image left) where gap starts.
    """
    try:
        import numpy as np
        from PIL import Image
        from scipy import ndimage

        bg_img = Image.open(io.BytesIO(bg_bytes)).convert("RGBA")
        pz_img = Image.open(io.BytesIO(puzzle_bytes)).convert("RGBA")

        bg = np.array(bg_img).astype(np.float32)
        pz = np.array(pz_img).astype(np.float32)

        pz_alpha = pz[:, :, 3]
        pz_mask = pz_alpha > 30

        bg_gray = np.mean(bg[:, :, :3], axis=2)
        pz_gray = np.mean(pz[:, :, :3], axis=2)

        ph, pw = pz_gray.shape
        bh, bw = bg_gray.shape

        # Pixel-by-pixel scan with edge weighting
        scores = []
        for x in range(0, bw - pw, 1):
            region = bg_gray[0:ph, x:x+pw]
            diff = np.abs(region - pz_gray)
            score = np.mean(diff[pz_mask]) if pz_mask.any() else np.mean(diff)
            scores.append((x, score))

        if not scores:
            return None

        scores.sort(key=lambda s: s[1])
        best_x, best_score = scores[0]

        # Sub-pixel refinement: fit parabola around best_x
        # using 3 adjacent score values
        idx = best_x
        if 1 <= idx <= len(scores) - 2:
            s_prev = next((s for x,s in scores if x == idx-1), best_score)
            s_curr = best_score
            s_next = next((s for x,s in scores if x == idx+1), best_score)
            denom = s_prev - 2*s_curr + s_next
            if abs(denom) > 1e-6:
                sub = -0.5 * (s_next - s_prev) / denom
                best_x_float = idx + sub
                log.info(f"Template match best_x={best_x} (sub={best_x_float:.2f}) score={best_score:.2f}")
                return best_x_float

        log.info(f"Template match best_x={best_x} score={best_score:.2f}")
        return float(best_x)
    except ImportError:
        # scipy not available — fall back to integer scan
        try:
            import numpy as np
            from PIL import Image
            bg_img = Image.open(io.BytesIO(bg_bytes)).convert("RGBA")
            pz_img = Image.open(io.BytesIO(puzzle_bytes)).convert("RGBA")
            bg = np.array(bg_img).astype(np.float32)
            pz = np.array(pz_img).astype(np.float32)
            pz_mask = pz[:,:,3] > 30
            bg_gray = np.mean(bg[:,:,:3], axis=2)
            pz_gray = np.mean(pz[:,:,:3], axis=2)
            ph, pw = pz_gray.shape
            bw = bg_gray.shape[1]
            best_score, best_x = float('inf'), 0
            for x in range(0, bw - pw):
                diff = np.abs(bg_gray[0:ph, x:x+pw] - pz_gray)
                score = np.mean(diff[pz_mask]) if pz_mask.any() else np.mean(diff)
                if score < best_score:
                    best_score, best_x = score, x
            log.info(f"Template match best_x={best_x} score={best_score:.2f}")
            return float(best_x)
        except Exception as e:
            log.warning(f"Template matching fallback failed: {e}")
            return None
    except Exception as e:
        log.warning(f"Template matching failed: {e}")
        return None


def _download(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read()
    except Exception as e:
        log.warning(f"Download failed {url}: {e}")
        return None


async def _find_gap_x_from_images(page, info: dict) -> float | None:
    """
    Try to determine drag distance using puzzle + background images.
    Returns absolute screen x of gap, or None.
    """
    puzzle_src = info.get('puzzleSrc')
    if not puzzle_src:
        return None

    # Try to get background image URL
    # Ozon's captcha often has bg as a data attribute or separate img element
    bg_src = info.get('captchaBgSrc')
    if not bg_src:
        # Try to find bg img in allImgs (the one that's NOT the puzzle)
        all_imgs = info.get('allImgs', [])
        for img in all_imgs:
            if img['src'] != puzzle_src and img['src']:
                bg_src = img['src']
                break

    if not bg_src:
        # Try to extract from page HTML
        bg_src = await page.evaluate("""
            () => {
                // Look for data-src or src on img elements in captcha
                const imgs = document.querySelectorAll('#captcha img:not(#puzzle), #captcha-container img:not(#puzzle)');
                for (const img of imgs) {
                    if (img.src) return img.src;
                    if (img.dataset.src) return img.dataset.src;
                }
                // Check style backgrounds
                const divs = document.querySelectorAll('#captcha div');
                for (const div of divs) {
                    const bg = window.getComputedStyle(div).backgroundImage;
                    if (bg && bg !== 'none' && bg.includes('http')) {
                        return bg.replace(/url[(]["']?/, '').replace(/["']?[)]$/, '');
                    }
                }
                return null;
            }
        """)

    log.info(f"Puzzle src: {puzzle_src}")
    log.info(f"BG src: {bg_src}")

    pz_bytes = _download(puzzle_src)
    if not pz_bytes:
        return None

    if bg_src:
        bg_bytes = _download(bg_src)
        if bg_bytes:
            gap_rel_x = _find_gap_by_template(bg_bytes, pz_bytes)
            if gap_rel_x is not None:
                from PIL import Image
                bg_img = Image.open(io.BytesIO(bg_bytes))
                pz_img = Image.open(io.BytesIO(pz_bytes))

                # Use bgImageRect for accurate scale (not captchaRect which is wider)
                bg_rect = info.get('bgImageRect') or info.get('captchaRect', {})
                bg_screen_w = bg_rect.get('w', bg_img.width)
                bg_screen_x = bg_rect.get('x', 703.5)

                # Scale: image pixels -> screen pixels
                sx = bg_screen_w / bg_img.width if bg_img.width > 0 else 1.0

                # Puzzle initial position in image coords
                puzzle = info.get('puzzle', {})
                puzzle_screen_x = puzzle.get('x', bg_screen_x)
                puzzle_img_x = (puzzle_screen_x - bg_screen_x) / sx

                # Drag in image coords, then convert to screen
                drag_img = gap_rel_x - puzzle_img_x
                drag_screen = drag_img * sx
                log.info(f"gap_img={gap_rel_x:.1f} puzzle_img={puzzle_img_x:.1f} drag_img={drag_img:.1f} sx={sx:.3f} drag_screen={drag_screen:.1f}px")
                return drag_screen

    return None


async def _human_drag(page, start_x: float, start_y: float, delta_x: float):
    await page.mouse.move(start_x, start_y)
    await asyncio.sleep(random.uniform(0.3, 0.7))
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.08, 0.18))

    steps = random.randint(45, 65)
    overshoot = random.uniform(3, 8)
    total = delta_x + overshoot

    for i in range(steps):
        t = (i + 1) / steps
        ease = t * t * (3 - 2 * t)
        if i < steps - 6:
            x = start_x + total * ease
        else:
            correction = overshoot * (1 - (i - (steps - 6)) / 6)
            x = start_x + (delta_x + correction) * ease
        y = start_y + random.uniform(-1.5, 1.5)
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.006, 0.022))

    await page.mouse.move(start_x + delta_x, start_y + random.uniform(-0.5, 0.5))
    await asyncio.sleep(random.uniform(0.15, 0.40))
    await page.mouse.up()


async def solve_slider(page) -> bool:
    try:
        info = await _get_slider_info(page)
        if not info or not info.get('slider'):
            log.warning("Slider DOM not found")
            return False

        slider = info['slider']
        bg = info['sliderBg']
        container = info.get('sliderContainer')
        puzzle = info.get('puzzle')

        log.info(f"Slider: x={slider['x']:.1f} y={slider['y']:.1f} w={slider['w']} h={slider['h']}")

        start_x = slider['x'] + slider['w'] / 2
        start_y = slider['y'] + slider['h'] / 2

        track_width = container['w'] if container and container.get('w', 0) > 10 else bg['w'] - slider['w'] - 16

        # Try image-based gap detection
        drag_delta = None
        if puzzle:
            drag_delta = await _find_gap_x_from_images(page, info)
            if drag_delta is not None:
                log.info(f"Image-based drag delta: {drag_delta:.1f}px")
                # Sanity check
                if drag_delta < 5 or drag_delta > track_width:
                    log.info(f"Delta {drag_delta:.1f} out of range [5, {track_width:.1f}], using fallback")
                    drag_delta = None

        if drag_delta is None:
            drag_delta = track_width * random.uniform(0.45, 0.75)
            log.info(f"Fallback drag delta: {drag_delta:.1f}px")

        drag_delta = min(max(drag_delta, 10), track_width - 4)

        await _human_drag(page, start_x, start_y, drag_delta)
        await asyncio.sleep(random.uniform(1.5, 2.5))

        # Check if slider resolved
        still_there = await page.query_selector('#slider')
        if not still_there:
            log.info("Slider gone — captcha likely solved")
            return True

        title = await page.title()
        if 'antibot' not in title.lower() and 'captcha' not in title.lower():
            log.info(f"Page title changed to: {title!r} — captcha resolved")
            return True

        log.info(f"Slider still present, title={title!r}")
        return False

    except Exception as e:
        log.warning(f"solve_slider error: {e}")
        return False
