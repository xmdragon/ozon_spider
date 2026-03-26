"""
Ozon slider captcha solver.

This version is geometry-first:
- extract the puzzle alpha contour bbox
- detect dark hole contours in the background image
- rank hole contours by shape similarity to the puzzle contour
- convert contour-left to screen drag distance in a consistent coordinate system
- try a few tight offsets around the best candidates
"""
import asyncio
import io
import logging
import random
import urllib.request

log = logging.getLogger(__name__)


async def _get_slider_info(page) -> dict | None:
    return await page.evaluate(
        """
        () => {
            const px = (value) => {
                const n = parseFloat(String(value || '').replace('px', '').trim());
                return Number.isFinite(n) ? n : null;
            };
            const vis = (el) => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 6 && r.height > 6 && s.visibility !== 'hidden' && s.display !== 'none';
            };
            const first = (sels) => {
                for (const sel of sels) {
                    for (const n of document.querySelectorAll(sel)) {
                        if (vis(n)) return n;
                    }
                }
                return null;
            };
            const toR = (el) => {
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return { x: r.x, y: r.y, w: r.width, h: r.height };
            };

            // Captcha slider handle is small (20-80px wide), not a full-width progress bar
            const sliderCandidates = [
                document.getElementById('slider'),
                ...Array.from(document.querySelectorAll('[id*="slider-handle"],[class*="slider-handle"],[class*="slider"] button,[class*="slider"] [role="button"]'))
            ].filter(el => {
                if (!el || !vis(el)) return false;
                const r = el.getBoundingClientRect();
                return r.width >= 20 && r.width <= 120 && r.height >= 15 && r.height <= 80;
            });
            const slider = sliderCandidates[0] || null;
            const bg = document.getElementById('slider-background') || first([
                '[class*="slider-background"]',
                '[class*="slider-track"]',
                '[class*="track"]',
            ]);
            const container = document.getElementById('slider-container') || first([
                '[class*="slider-container"]',
            ]);
            const puzzle = document.getElementById('puzzle') || first([
                'img[src*="puzzle"]',
                '[class*="puzzle"] img',
            ]);
            const captcha = document.getElementById('captcha') || first([
                '#captcha',
                '#captcha-container > div',
                '[class*="captcha"]',
            ]);
            const bgImgEl = document.getElementById('image') || first([
                '#captcha img:not([src*="puzzle"])',
                '#captcha-container img:not([src*="puzzle"])',
                '[class*="captcha"] img:not([src*="puzzle"])',
            ]);

            if (!slider || !bg) return null;

            const allImgs = [];
            document.querySelectorAll('#captcha img,#captcha-container img,[class*="captcha"] img').forEach((img) => {
                allImgs.push({
                    src: img.src,
                    id: img.id,
                    cls: img.className,
                    w: img.naturalWidth,
                    h: img.naturalHeight,
                });
            });

            let captchaBgSrc = bgImgEl ? bgImgEl.src : null;
            if (!captchaBgSrc) {
                const cap = document.querySelector('#captcha,#captcha-container,[class*="captcha"]');
                if (cap) {
                    const v = window.getComputedStyle(cap).backgroundImage;
                    if (v && v !== 'none') {
                        captchaBgSrc = v.replace(/url[(]["']?/, '').replace(/["']?[)]$/, '');
                    }
                }
            }

            const puzzleStyle = puzzle ? window.getComputedStyle(puzzle) : null;
            const captchaStyle = captcha ? window.getComputedStyle(captcha) : null;
            const cssScale = captchaStyle ? px(captchaStyle.getPropertyValue('--scale')) : null;
            const styleScale = captcha && captcha.style ? px(captcha.style.getPropertyValue('--scale')) : null;
            const scale = styleScale || cssScale || 1;

            return {
                slider: toR(slider),
                sliderBg: toR(bg),
                sliderContainer: toR(container),
                captcha: toR(captcha),
                puzzle: toR(puzzle),
                puzzleSrc: puzzle ? puzzle.src : null,
                puzzleNatural: puzzle ? { w: puzzle.naturalWidth, h: puzzle.naturalHeight } : null,
                puzzleCssLeft: puzzleStyle ? px(puzzleStyle.left) : null,
                puzzleCssTop: puzzleStyle ? px(puzzleStyle.top) : null,
                captchaScale: scale,
                bgImageRect: toR(bgImgEl),
                captchaBgSrc,
                allImgs,
            };
        }
        """
    )


async def _wait_for_slider_ready(page, timeout: float = 10.0) -> dict | None:
    deadline = asyncio.get_event_loop().time() + timeout
    last = None
    while asyncio.get_event_loop().time() < deadline:
        info = await _get_slider_info(page)
        last = info
        if info:
            slider = info.get("slider") or {}
            bg = info.get("sliderBg") or {}
            puzzle = info.get("puzzle") or {}
            bg_img = info.get("bgImageRect") or {}
            if (
                slider.get("w", 0) > 5
                and slider.get("h", 0) > 5
                and bg.get("w", 0) > 20
                and bg.get("h", 0) > 20
                and puzzle.get("w", 0) > 5
                and puzzle.get("h", 0) > 5
                and bg_img.get("w", 0) > 20
                and bg_img.get("h", 0) > 20
                and info.get("puzzleSrc")
                and info.get("captchaBgSrc")
            ):
                return info
        await asyncio.sleep(0.2)
    return last


def _download(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read()
    except Exception as e:
        log.warning("Download failed %s: %s", url, e)
        return None


def _find_gap_candidates(bg_bytes: bytes, puzzle_bytes: bytes, puzzle_img_x: float = 0.0, puzzle_img_y: float = -1.0) -> list[dict]:
    """
    Find the gap position in the background image.

    Strategy:
    - Photo background (std > 15): use B-R colour score inside puzzle mask,
      scanning only x >= puzzle's left edge (gap is always to the right).
    - Uniform/checkerboard background (std <= 15): Canny edge matchTemplate
      as fallback.
    Returns list of candidate dicts sorted best-first.
    """
    try:
        import cv2
        import numpy as np
        from PIL import Image

        def side_signature(mask_u8) -> dict[str, int]:
            fg = mask_u8 > 0
            h, w = fg.shape[:2]
            if h < 12 or w < 12:
                return {"l": 0, "r": 0, "t": 0, "b": 0}

            def smooth(values: list[float]) -> np.ndarray:
                arr = np.array(values, dtype=np.float32)
                if arr.size < 5:
                    return arr
                kernel = np.ones(5, dtype=np.float32) / 5.0
                return np.convolve(arr, kernel, mode="same")

            left_profile = []
            right_profile = []
            for row in fg:
                xs = np.flatnonzero(row)
                if xs.size:
                    left_profile.append(float(xs[0]))
                    right_profile.append(float(xs[-1]))

            top_profile = []
            bottom_profile = []
            for col in fg.T:
                ys = np.flatnonzero(col)
                if ys.size:
                    top_profile.append(float(ys[0]))
                    bottom_profile.append(float(ys[-1]))

            def classify(profile, length: float, smaller_is_outward: bool) -> int:
                if len(profile) < 8:
                    return 0
                arr = smooth(profile)
                n = len(arr)

                # Compare side shoulders to a wider center band. This is more
                # tolerant of off-center bumps/notches than using a single middle
                # slice, while still ignoring corner noise from neighbouring sides.
                a0 = max(0, int(n * 0.14))
                a1 = max(a0 + 1, int(n * 0.30))
                c0 = max(0, int(n * 0.28))
                c1 = max(c0 + 1, int(n * 0.72))
                b0 = max(0, int(n * 0.70))
                b1 = max(b0 + 1, int(n * 0.86))

                outer = np.concatenate([arr[a0:a1], arr[b0:b1]])
                center = arr[c0:c1]
                if center.size == 0 or outer.size == 0:
                    return 0
                baseline = float(np.median(outer))
                delta = baseline - center if smaller_is_outward else center - baseline
                pos = float(np.quantile(delta, 0.8))
                neg = float(np.quantile(delta, 0.2))
                threshold = max(2.0, length * 0.06)
                if pos > threshold and pos > abs(neg) * 1.35:
                    return 1
                if neg < -threshold and abs(neg) > pos * 1.35:
                    return -1
                return 0

            return {
                "l": classify(left_profile, w, True),
                "r": classify(right_profile, w, False),
                "t": classify(top_profile, h, True),
                "b": classify(bottom_profile, h, False),
            }

        def signature_text(sig: dict[str, int]) -> str:
            parts = [f"{k}:{v}" for k, v in sig.items() if v]
            return "{" + ",".join(parts) + "}" if parts else "{}"

        bg_img = Image.open(io.BytesIO(bg_bytes)).convert("RGB")
        pz_img = Image.open(io.BytesIO(puzzle_bytes)).convert("RGBA")
        bg = np.array(bg_img)
        pz = np.array(pz_img)
        bh, bw = bg.shape[:2]

        alpha = pz[:, :, 3]
        mask = alpha > 30
        if not mask.any():
            return []
        ph, pw = mask.shape
        ys, xs = np.where(mask)
        left = int(xs.min())
        top = int(ys.min())
        right = int(xs.max())
        bottom = int(ys.max())
        bbox_w = right - left + 1
        bbox_h = bottom - top + 1

        puzzle_mask_u8 = (mask.astype(np.uint8) * 255)
        puzzle_bbox_mask_u8 = puzzle_mask_u8[top:bottom + 1, left:right + 1]
        puzzle_contours, _ = cv2.findContours(puzzle_mask_u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        puzzle_contour = max(puzzle_contours, key=cv2.contourArea) if puzzle_contours else None
        puzzle_sig = side_signature(puzzle_bbox_mask_u8)
        required_match_count = sum(1 for key in ("l", "r", "t", "b") if puzzle_sig[key] != 0)

        # Stable green-box pipeline:
        # 1. remove dominant background colours
        # 2. keep only connected block-like components
        # 3. later restrict them to the red-line row band and right side
        #
        # This was introduced after live validation showed that operating on the
        # raw background image was too noisy: diagonal stripes and background
        # gradients polluted the candidate pool.
        def color_component_candidates(bg_rgb: np.ndarray) -> list[dict]:
            flat = bg_rgb.reshape((-1, 3)).astype(np.float32)
            k = 5
            criteria = (
                cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER,
                30,
                0.5,
            )
            _compactness, labels, centers = cv2.kmeans(
                flat,
                k,
                None,
                criteria,
                8,
                cv2.KMEANS_PP_CENTERS,
            )
            labels = labels.reshape((bh, bw))

            counts = []
            for idx in range(k):
                count = int((labels == idx).sum())
                counts.append({"idx": idx, "count": count})
            counts.sort(key=lambda item: item["count"], reverse=True)

            removed_idxs = [counts[0]["idx"]]
            if len(counts) > 1:
                removed_idxs.append(counts[1]["idx"])

            filtered = bg_rgb.copy()
            for idx in removed_idxs:
                filtered[labels == idx] = [255, 255, 255]

            fg_mask = (np.any(filtered < 245, axis=2).astype(np.uint8) * 255)
            num_labels, cc_labels, stats, _ = cv2.connectedComponentsWithStats(fg_mask, connectivity=8)

            components = []
            for label in range(1, num_labels):
                x, y, w, h, area = stats[label].tolist()
                if area < 300:
                    continue
                aspect = max(w / max(h, 1), h / max(w, 1))
                touches_full_height = h > bh * 0.55
                thin_component = min(w, h) <= 8
                likely_diagonal = touches_full_height and thin_component
                too_large = area > bh * bw * 0.18
                if likely_diagonal or too_large or aspect > 14:
                    continue
                comp_mask = np.zeros((h, w), dtype=np.uint8)
                comp_mask[cc_labels[y:y + h, x:x + w] == label] = 255
                components.append(
                    {
                        "x": float(x),
                        "y": float(y),
                        "w": float(w),
                        "h": float(h),
                        "mask": comp_mask,
                    }
                )

            # Some captcha tiles get split into neighbouring blobs after colour
            # filtering. Merge close, same-row components so signature extraction
            # sees one block instead of two half-shapes.
            merged = list(components)
            merge_gap = max(6, int(round(bbox_w * 0.18)))
            for i, a in enumerate(components):
                ax1, ay1 = int(a["x"]), int(a["y"])
                ax2, ay2 = ax1 + int(a["w"]), ay1 + int(a["h"])
                for j, b in enumerate(components):
                    if j <= i:
                        continue
                    bx1, by1 = int(b["x"]), int(b["y"])
                    bx2, by2 = bx1 + int(b["w"]), by1 + int(b["h"])
                    overlap_y = min(ay2, by2) - max(ay1, by1)
                    if overlap_y < min(a["h"], b["h"]) * 0.55:
                        continue
                    horizontal_gap = max(0, max(ax1, bx1) - min(ax2, bx2))
                    if horizontal_gap > merge_gap:
                        continue
                    mx1, my1 = min(ax1, bx1), min(ay1, by1)
                    mx2, my2 = max(ax2, bx2), max(ay2, by2)
                    mw, mh = mx2 - mx1, my2 - my1
                    mmask = np.zeros((mh, mw), dtype=np.uint8)
                    mmask[ay1 - my1: ay1 - my1 + int(a["h"]), ax1 - mx1: ax1 - mx1 + int(a["w"])] |= a["mask"]
                    mmask[by1 - my1: by1 - my1 + int(b["h"]), bx1 - mx1: bx1 - mx1 + int(b["w"])] |= b["mask"]
                    merged.append(
                        {
                            "x": float(mx1),
                            "y": float(my1),
                            "w": float(mw),
                            "h": float(mh),
                            "mask": mmask,
                        }
                    )
            return merged

        # Detect background type
        bg_gray = cv2.cvtColor(bg, cv2.COLOR_RGB2GRAY).astype(np.float32)
        bg_std = float(bg_gray.std())
        log.info("BG std=%.1f (photo>15)", bg_std)

        # Preferred green-box strategy:
        # - use the puzzle style.top/style.left row as the authoritative band
        # - search only inside colour-filtered connected components
        # - prefer exact signature matches on the right
        # - if none exist, allow signature-cover matches
        # - as a final fallback, pick the rightmost right-side block candidate
        #
        # Do not mix this with the red/orange-box coordinate baseline above.
        if puzzle_img_y >= 0 and puzzle_contour is not None:
            row_y1 = max(0, min(int(round(puzzle_img_y)), bh - ph))
            row_y2 = max(row_y1 + 1, min(bh, row_y1 + ph))
            components = color_component_candidates(bg)
            current_left = float(puzzle_img_x)
            current_top = float(puzzle_img_y)
            current_bottom = current_top + float(ph)
            row_candidates = []

            for comp in components:
                x = float(comp["x"])
                y = float(comp["y"])
                w = float(comp["w"])
                h = float(comp["h"])

                # Candidate must materially overlap the current puzzle row band.
                overlap_y = min(current_bottom, y + h) - max(current_top, y)
                if overlap_y < min(bbox_h, h) * 0.35:
                    continue

                candidate_mask = comp["mask"]
                candidate_sig = side_signature(candidate_mask)
                candidate_contours, _ = cv2.findContours(candidate_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                if not candidate_contours:
                    continue
                candidate_contour = max(candidate_contours, key=cv2.contourArea)
                shape_score = float(cv2.matchShapes(puzzle_contour, candidate_contour, cv2.CONTOURS_MATCH_I1, 0.0))
                vertical_delta = abs(y - current_top)
                size_score = abs(w - bbox_w) / max(1.0, bbox_w) + abs(h - bbox_h) / max(1.0, bbox_h)
                feature_score = sum(abs(candidate_sig[k] - puzzle_sig[k]) for k in ("l", "r", "t", "b")) / 2.0
                score = shape_score + size_score * 0.35 + vertical_delta / max(1.0, bbox_h) * 0.15 + feature_score * 0.6
                candidate = {
                    "gap_x": float(x),
                    "gap_y": float(y),
                    "gap_w": float(w),
                    "gap_h": float(h),
                    "score": score,
                    "shape_score": shape_score,
                    "size_score": size_score,
                    "feature_score": feature_score,
                    "sig_match_count": sum(
                        1 for k in ("l", "r", "t", "b")
                        if puzzle_sig[k] != 0 and candidate_sig[k] == puzzle_sig[k]
                    ),
                    "puzzle_signature": puzzle_sig,
                    "candidate_signature": candidate_sig,
                    "signature_text": signature_text(candidate_sig),
                    "puzzle_signature_text": signature_text(puzzle_sig),
                    "bbox_left": float(left),
                    "bbox_top": float(top),
                    "bbox_w": float(bbox_w),
                    "bbox_h": float(bbox_h),
                    "puzzle_img_w": float(pz_img.width),
                    "puzzle_img_h": float(pz_img.height),
                }
                # "row_candidates" are the normal pool: similar size, same-row blocks.
                if w >= bbox_w * 0.55 and h >= bbox_h * 0.55 and w <= bbox_w * 1.60 and h <= bbox_h * 1.60:
                    row_candidates.append(candidate)

            if row_candidates:
                row_candidates.sort(key=lambda item: (-item["gap_x"], item["score"]))
                exact = [
                    c for c in row_candidates
                    if c["gap_x"] > current_left and c["candidate_signature"] == puzzle_sig
                ]
                if exact:
                    selected = exact
                else:
                    # Signature-cover match: every non-zero puzzle edge feature must
                    # appear on the candidate, even if the candidate has extra edges.
                    selected = [
                        c for c in row_candidates
                        if c["gap_x"] > current_left and c["sig_match_count"] >= required_match_count
                    ]
                if not selected:
                    log.info(
                        "Gap components row=(%d,%d) has no exact/signature-cover match. puzzle_sig=%s candidates=%s",
                        row_y1,
                        row_y2,
                        signature_text(puzzle_sig),
                        [
                            {
                                "x": int(c["gap_x"]),
                                "y": int(c["gap_y"]),
                                "sig": c["signature_text"],
                                "match": c["sig_match_count"],
                                "need": required_match_count,
                                "score": round(c["score"], 3),
                            }
                            for c in row_candidates[:6]
                        ],
                    )
                    return []
                log.info(
                    "Gap component candidates row=(%d,%d) current_left=%.1f puzzle_sig=%s: %s",
                    row_y1,
                    row_y2,
                    current_left,
                    signature_text(puzzle_sig),
                    [
                        {
                            "x": int(c["gap_x"]),
                            "y": int(c["gap_y"]),
                            "w": int(c["gap_w"]),
                            "h": int(c["gap_h"]),
                            "score": round(c["score"], 3),
                            "shape": round(c["shape_score"], 3),
                            "size": round(c["size_score"], 3),
                            "feature": round(c["feature_score"], 3),
                            "match": c["sig_match_count"],
                            "sig": c["signature_text"],
                        }
                        for c in selected[:5]
                    ],
                )
                for idx, candidate in enumerate(selected[:3]):
                    candidate["scan_order"] = idx
                return selected[:3]

        # --- Method 1: Canny edge matchTemplate ---
        pz_gray = cv2.cvtColor(pz[:, :, :3], cv2.COLOR_RGB2GRAY).astype(np.uint8)
        pz_gray[~mask] = 0
        bg_edge = cv2.Canny(bg_gray.astype(np.uint8), 50, 150)
        pz_edge = cv2.Canny(pz_gray, 50, 150)
        pz_edge[~mask] = 0
        res = cv2.matchTemplate(bg_edge, pz_edge, cv2.TM_CCOEFF_NORMED)
        _, canny_score, _, canny_loc = cv2.minMaxLoc(res)
        log.info("Canny match: x=%d y=%d score=%.3f", canny_loc[0], canny_loc[1], canny_score)

        # --- Method 2: B-R colour score with fixed Y (works on flat-colour backgrounds) ---
        br_x, br_score = canny_loc[0], -1.0
        if puzzle_img_y >= 0:
            y_fixed = max(0, min(int(puzzle_img_y), bh - ph))
            min_x = max(0, int(puzzle_img_x))
            R = bg[:, :, 0].astype(np.float32)
            B = bg[:, :, 2].astype(np.float32)
            br_map = B - R
            best_br, best_br_x = -999.0, min_x
            for x in range(min_x, bw - pw + 1):
                s = float(br_map[y_fixed:y_fixed+ph, x:x+pw][mask].mean())
                if s > best_br:
                    best_br = s; best_br_x = x
            br_x = best_br_x
            br_score = best_br
            log.info("B-R match: x=%d score=%.1f (y_fixed=%d)", br_x, br_score, y_fixed)

        # Choose: use Canny if score >= 0.5, else use B-R
        if canny_score >= 0.5:
            gap_x, gap_y = float(canny_loc[0]), float(canny_loc[1])
            log.info("Using Canny (score=%.3f)", canny_score)
        else:
            gap_x = float(br_x)
            gap_y = float(puzzle_img_y) if puzzle_img_y >= 0 else float(canny_loc[1])
            log.info("Using B-R (canny_score=%.3f < 0.5)", canny_score)

        if canny_score < 0.1 and br_score < 0:
            return []
        return [{
            "gap_x": gap_x,
            "gap_y": gap_y,
            "gap_w": float(pw),
            "gap_h": float(ph),
            "score": float(1.0 - max(canny_score, 0.0)),
            "shape_score": 0.0,
            "size_score": 0.0,
            "feature_score": 0.0,
            "scan_order": 999,
            "puzzle_signature": puzzle_sig,
            "candidate_signature": puzzle_sig,
            "signature_text": signature_text(puzzle_sig),
            "puzzle_signature_text": signature_text(puzzle_sig),
            "bbox_left": float(left),
            "bbox_top": float(top),
            "bbox_w": float(bbox_w),
            "bbox_h": float(bbox_h),
            "puzzle_img_w": float(pz_img.width),
            "puzzle_img_h": float(pz_img.height),
        }]
    except Exception as e:
        log.warning("Gap detection error: %s", e)
        return []


async def _compute_drag_candidates(page, info: dict) -> list[dict]:
    puzzle_src = info.get("puzzleSrc")
    if not puzzle_src:
        log.warning("No puzzle src")
        return []

    bg_src = info.get("captchaBgSrc")
    if not bg_src:
        for img in info.get("allImgs", []):
            src = img.get("src", "")
            if src and src != puzzle_src and "puzzle" not in src.lower():
                bg_src = src
                break
    if not bg_src:
        bg_src = await page.evaluate(
            """
            () => {
                for (const img of document.querySelectorAll('#captcha img,#captcha-container img,[class*="captcha"] img')) {
                    const src = img.src || img.dataset.src || '';
                    if (src && !src.includes('puzzle')) return src;
                }
                return null;
            }
            """
        )

    log.info("BG src:     %s", bg_src)
    log.info("Puzzle src: %s", puzzle_src)
    if not bg_src:
        log.warning("No background image found")
        return []

    puzzle_bytes = _download(puzzle_src)
    bg_bytes = _download(bg_src)
    if not puzzle_bytes or not bg_bytes:
        return []

    from PIL import Image

    def _compute_puzzle_dom_box() -> tuple[dict, float, float, dict, float, float]:
        bg_img = Image.open(io.BytesIO(bg_bytes))
        bg_img_w = float(bg_img.width)
        bg_rect_local = info.get("bgImageRect") or info.get("sliderBg") or {}
        bg_screen_x_local = float(bg_rect_local.get("x", 0))
        bg_screen_y_local = float(bg_rect_local.get("y", 0))
        bg_screen_w_local = float(bg_rect_local.get("w", bg_img_w))
        bg_scale_local = bg_screen_w_local / bg_img_w if bg_img_w > 0 else 1.0

        puzzle_local = info.get("puzzle") or {}
        puzzle_screen_x_local = float(puzzle_local.get("x", bg_screen_x_local))
        puzzle_screen_y_local = float(puzzle_local.get("y", bg_screen_y_local))
        puzzle_screen_w_local = float(puzzle_local.get("w", 0))
        puzzle_screen_h_local = float(puzzle_local.get("h", 0))
        captcha_local = info.get("captcha") or {}
        captcha_screen_x_local = float(captcha_local.get("x", bg_screen_x_local))
        captcha_screen_y_local = float(captcha_local.get("y", bg_screen_y_local))
        captcha_scale_local = float(info.get("captchaScale") or bg_scale_local or 1.0)
        puzzle_css_left_local = info.get("puzzleCssLeft")
        puzzle_css_top_local = info.get("puzzleCssTop")
        puzzle_natural_local = info.get("puzzleNatural") or {}
        puzzle_natural_w_local = float(puzzle_natural_local.get("w") or 0.0)
        puzzle_natural_h_local = float(puzzle_natural_local.get("h") or 0.0)

        if bg_screen_w_local <= 0:
            bg_screen_w_local = float((info.get("sliderBg") or {}).get("w", bg_img_w))
            bg_scale_local = bg_screen_w_local / bg_img_w if bg_img_w > 0 else 1.0

        # Stable baseline, verified interactively by the user:
        # use the captcha container origin + puzzle style.left/style.top + --scale
        # as the authoritative DOM coordinate chain for the current piece position.
        if puzzle_css_left_local is not None and puzzle_css_top_local is not None:
            puzzle_img_x_local = float(puzzle_css_left_local)
            puzzle_img_y_local = float(puzzle_css_top_local)
        else:
            puzzle_img_x_local = (puzzle_screen_x_local - bg_screen_x_local) / bg_scale_local if bg_scale_local > 0 else 0.0
            puzzle_img_y_local = (puzzle_screen_y_local - bg_screen_y_local) / bg_scale_local if bg_scale_local > 0 else -1.0

        # Stable orange-box / red-line visual baseline. Keep this aligned with
        # the style.top/style.left chain above so the DOM math and debug overlay
        # cannot silently diverge.
        if puzzle_css_left_local is not None and puzzle_natural_w_local > 0:
            puzzle_outer_x_local = captcha_screen_x_local + float(puzzle_css_left_local) * captcha_scale_local
            puzzle_outer_y_local = captcha_screen_y_local + float(puzzle_css_top_local or 0.0) * captcha_scale_local
            puzzle_outer_w_local = puzzle_natural_w_local * captcha_scale_local
            puzzle_outer_h_local = puzzle_natural_h_local * captcha_scale_local if puzzle_natural_h_local > 0 else puzzle_screen_h_local
        else:
            puzzle_outer_x_local = puzzle_screen_x_local
            puzzle_outer_y_local = puzzle_screen_y_local
            puzzle_outer_w_local = puzzle_screen_w_local
            puzzle_outer_h_local = puzzle_screen_h_local

        return (
            {
                "x": float(puzzle_outer_x_local),
                "y": float(puzzle_outer_y_local),
                "w": float(puzzle_outer_w_local),
                "h": float(puzzle_outer_h_local),
            },
            float(puzzle_img_x_local),
            float(puzzle_img_y_local),
            {
                "x": float(bg_screen_x_local),
                "y": float(bg_screen_y_local),
                "w": float(bg_screen_w_local),
                "scale": float(bg_scale_local),
            },
            float(puzzle_screen_x_local),
            float(puzzle_screen_y_local),
        )

    def _compute_debug_row(puzzle_box: dict) -> tuple[float, float]:
        return float(puzzle_box["y"]), float(puzzle_box["y"] + puzzle_box["h"])

    def _compute_target_debug_box(target_outer_left: float, puzzle_box: dict) -> dict:
        return {
            "x": float(target_outer_left),
            "y": float(puzzle_box["y"]),
            "w": float(puzzle_box["w"]),
            "h": float(puzzle_box["h"]),
        }

    puzzle_box, puzzle_img_x, puzzle_img_y, bg_screen, puzzle_screen_x, puzzle_screen_y = _compute_puzzle_dom_box()
    puzzle_outer_x = float(puzzle_box["x"])
    puzzle_outer_y = float(puzzle_box["y"])
    puzzle_outer_w = float(puzzle_box["w"])
    puzzle_outer_h = float(puzzle_box["h"])
    bg_screen_x = float(bg_screen["x"])
    bg_screen_y = float(bg_screen["y"])
    bg_scale = float(bg_screen["scale"])
    puzzle = info.get("puzzle") or {}
    puzzle_screen_w = float(puzzle.get("w", 0))
    puzzle_screen_h = float(puzzle.get("h", 0))

    log.info(
        "puzzle_img=(%.1f,%.1f) bg_scale=%.3f captcha_scale=%.3f css=(%s,%s) outer=(%.1f,%.1f,%.1f,%.1f)",
        puzzle_img_x,
        puzzle_img_y,
        bg_scale,
        float(info.get("captchaScale") or bg_scale or 1.0),
        info.get("puzzleCssLeft"),
        info.get("puzzleCssTop"),
        puzzle_outer_x,
        puzzle_outer_y,
        puzzle_outer_w,
        puzzle_outer_h,
    )
    gap_candidates = _find_gap_candidates(bg_bytes, puzzle_bytes, puzzle_img_x=puzzle_img_x, puzzle_img_y=puzzle_img_y)
    if not gap_candidates:
        return []

    # The moving puzzle stays on the same vertical lane; use that as a strong prior.
    ranked = []
    for gap in gap_candidates:
        puzzle_img_w = float(gap["puzzle_img_w"]) or 1.0
        puzzle_img_h = float(gap["puzzle_img_h"]) or 1.0
        bbox_left = float(gap["bbox_left"])
        bbox_top = float(gap["bbox_top"])
        bbox_w = float(gap.get("bbox_w") or puzzle_img_w)
        bbox_h = float(gap.get("bbox_h") or puzzle_img_h)
        gap_x = float(gap["gap_x"])
        gap_y = float(gap["gap_y"])

        puzzle_scale_x = puzzle_screen_w / puzzle_img_w if puzzle_img_w > 0 and puzzle_screen_w > 0 else bg_scale
        puzzle_scale_y = puzzle_screen_h / puzzle_img_h if puzzle_img_h > 0 and puzzle_screen_h > 0 else puzzle_scale_x
        # Current puzzle position is the outer image box in captcha image coordinates.
        # gap_x/gap_y is the matched candidate bbox in bg image coordinates.
        #
        # For the green debug box we want the candidate piece to sit inside the
        # box the same way the real puzzle sits inside the orange box.  Left-edge
        # alignment is visually biased when the candidate bbox width differs a bit
        # from the puzzle opaque bbox width, so align by contour center instead.
        contour_center_offset_x = bbox_left + bbox_w / 2.0
        target_outer_left_img = (gap_x + gap["gap_w"] / 2.0) - contour_center_offset_x
        target_outer_top_img = gap_y - bbox_top
        drag = (target_outer_left_img - puzzle_img_x) * bg_scale
        target_outer_left = bg_screen_x + target_outer_left_img * bg_scale
        target_outer_top = bg_screen_y + target_outer_top_img * bg_scale
        target_contour_left = bg_screen_x + gap_x * bg_scale
        target_contour_top = bg_screen_y + gap_y * bg_scale
        current_contour_left = puzzle_screen_x + bbox_left * puzzle_scale_x
        current_contour_top = puzzle_screen_y + bbox_top * puzzle_scale_y
        current_contour_w = bbox_w * puzzle_scale_x
        current_contour_h = bbox_h * puzzle_scale_y
        vertical_delta = abs(target_contour_top - current_contour_top)
        if target_outer_left <= puzzle_outer_x + 2:
            log.info(
                "Discarding left/non-right target: puzzle_outer_x=%.1f target_outer_left=%.1f",
                puzzle_outer_x,
                target_outer_left,
            )
            continue
        log.info(
            "gap_img=(%.1f,%.1f) bbox=(%.1f,%.1f,%.1f,%.1f) bg_scale=%.3f puzzle_scale=(%.3f,%.3f) current=(%.1f,%.1f,%.1f,%.1f) target_outer=(%.1f,%.1f) target_contour=(%.1f,%.1f) vertical_delta=%.1f => drag=%.1fpx",
            gap_x,
            gap_y,
            bbox_left,
            bbox_top,
            bbox_w,
            bbox_h,
            bg_scale,
            puzzle_scale_x,
            puzzle_scale_y,
            current_contour_left,
            current_contour_top,
            current_contour_w,
            current_contour_h,
            target_outer_left,
            target_outer_top,
            target_contour_left,
            target_contour_top,
            vertical_delta,
            drag,
        )
        ranked.append({
            "scan_order": int(gap.get("scan_order", 999)),
            "vertical_delta": float(vertical_delta),
            "score": float(gap["score"]),
            "drag": float(drag),
            "target_left_img": float(target_outer_left_img),
            "puzzle_signature_text": gap.get("puzzle_signature_text", "{}"),
            "candidate_signature_text": gap.get("signature_text", "{}"),
            "row_y1": _compute_debug_row(puzzle_box)[0],
            "row_y2": _compute_debug_row(puzzle_box)[1],
            "puzzle_box": puzzle_box,
            "target_box": _compute_target_debug_box(target_outer_left, puzzle_box),
            "current_contour_w": float(current_contour_w),
        })

    ranked.sort(key=lambda item: (item["scan_order"], item["score"]))
    return ranked


async def _draw_debug_overlay(page, row_y1: float, row_y2: float, target_box: dict | None, puzzle_box: dict | None, note: str):
    await page.evaluate(
        """
        ([rowY1, rowY2, targetBox, puzzleBox, note]) => {
            const old = document.getElementById('__slider-debug-overlay__');
            if (old) old.remove();

            const root = document.createElement('div');
            root.id = '__slider-debug-overlay__';
            root.style.position = 'fixed';
            root.style.left = '0';
            root.style.top = '0';
            root.style.width = '100vw';
            root.style.height = '100vh';
            root.style.pointerEvents = 'none';
            root.style.zIndex = '2147483647';

            const makeLine = (y) => {
                const line = document.createElement('div');
                line.style.position = 'fixed';
                line.style.left = '0';
                line.style.top = `${y}px`;
                line.style.width = '100vw';
                line.style.height = '2px';
                line.style.background = 'red';
                line.style.boxShadow = '0 0 6px rgba(255,0,0,0.8)';
                return line;
            };

            root.appendChild(makeLine(rowY1));
            root.appendChild(makeLine(rowY2));

            if (targetBox) {
                const box = document.createElement('div');
                box.style.position = 'fixed';
                box.style.left = `${targetBox.x}px`;
                box.style.top = `${targetBox.y}px`;
                box.style.width = `${targetBox.w}px`;
                box.style.height = `${targetBox.h}px`;
                box.style.border = '3px solid #00ff4c';
                box.style.boxSizing = 'border-box';
                box.style.boxShadow = '0 0 8px rgba(0,255,76,0.9)';
                root.appendChild(box);
            }

            if (puzzleBox) {
                const box = document.createElement('div');
                box.style.position = 'fixed';
                box.style.left = `${puzzleBox.x}px`;
                box.style.top = `${puzzleBox.y}px`;
                box.style.width = `${puzzleBox.w}px`;
                box.style.height = `${puzzleBox.h}px`;
                box.style.border = '3px solid orange';
                box.style.boxSizing = 'border-box';
                box.style.boxShadow = '0 0 8px rgba(255,165,0,0.9)';
                root.appendChild(box);
            }

            const label = document.createElement('div');
            label.textContent = note;
            label.style.position = 'fixed';
            label.style.left = '16px';
            label.style.top = '16px';
            label.style.padding = '8px 12px';
            label.style.background = 'rgba(0,0,0,0.7)';
            label.style.color = '#fff';
            label.style.font = '13px monospace';
            label.style.whiteSpace = 'pre-wrap';
            label.style.maxWidth = '420px';
            label.style.borderRadius = '6px';
            label.style.border = '1px solid rgba(255,255,255,0.2)';
            root.appendChild(label);

            document.body.appendChild(root);
        }
        """,
        [row_y1, row_y2, target_box, puzzle_box, note],
    )

async def _human_drag(page, sx: float, sy: float, dx: float):
    # Approach the slider naturally from somewhere above/left
    start_x = sx - random.uniform(80, 200)
    start_y = sy - random.uniform(30, 80)
    await page.mouse.move(float(start_x), float(start_y))
    await asyncio.sleep(random.uniform(0.1, 0.3))
    # Move toward slider handle in a slight curve
    mid_x = sx - random.uniform(10, 30)
    mid_y = sy + random.uniform(-5, 5)
    await page.mouse.move(float(mid_x), float(mid_y))
    await asyncio.sleep(random.uniform(0.05, 0.15))
    await page.mouse.move(float(sx), float(sy))
    await asyncio.sleep(random.uniform(0.3, 0.7))
    # Small hover jitter before pressing
    for _ in range(random.randint(1, 3)):
        await page.mouse.move(float(sx + random.uniform(-2, 2)), float(sy + random.uniform(-2, 2)))
        await asyncio.sleep(random.uniform(0.05, 0.12))
    await page.mouse.move(float(sx), float(sy))
    await asyncio.sleep(random.uniform(0.1, 0.2))
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.06, 0.15))

    total_t = random.uniform(1.0, 2.0)
    steps = random.randint(50, 80)
    dt = total_t / steps
    over = random.uniform(2, 6)

    for i in range(steps):
        t = (i + 1) / steps
        ease = t * t * (3 - 2 * t)
        pull = max(0.0, (t - 0.85) / 0.15)
        target = dx + over * (1 - pull)
        await page.mouse.move(float(sx + target * ease), float(sy + random.uniform(-1.5, 1.5)))
        await asyncio.sleep(dt * random.uniform(0.7, 1.3))

    await page.mouse.move(float(sx + dx), float(sy + random.uniform(-0.5, 0.5)))
    await asyncio.sleep(random.uniform(0.1, 0.3))
    await page.mouse.up()


async def _get_puzzle_css_left(page) -> float | None:
    try:
        value = await page.evaluate(
            """
            () => {
                const puzzle = document.getElementById('puzzle');
                if (!puzzle) return null;
                const style = window.getComputedStyle(puzzle);
                const raw = style.left || puzzle.style.left || '';
                const n = parseFloat(String(raw).replace('px', '').trim());
                return Number.isFinite(n) ? n : null;
            }
            """
        )
        return float(value) if value is not None else None
    except Exception:
        return None


async def _drag_until_target_left(page, sx: float, sy: float, target_left_img: float, tolerance: float = 5.0) -> bool:
    current_left = await _get_puzzle_css_left(page)
    if current_left is None:
        return False

    start_x = sx - random.uniform(80, 200)
    start_y = sy - random.uniform(30, 80)
    await page.mouse.move(float(start_x), float(start_y))
    await asyncio.sleep(random.uniform(0.1, 0.25))
    await page.mouse.move(float(sx), float(sy))
    await asyncio.sleep(random.uniform(0.1, 0.2))
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.06, 0.15))

    last_left = current_left
    step_px = 8.0
    max_steps = 80
    for _ in range(max_steps):
        current_left = await _get_puzzle_css_left(page)
        if current_left is None:
            break
        if abs(current_left - target_left_img) <= tolerance:
            await asyncio.sleep(random.uniform(0.05, 0.12))
            await page.mouse.up()
            return True

        # If DOM left is not advancing anymore, release early to avoid overshooting.
        if current_left + 1.0 < last_left:
            break

        remaining = target_left_img - current_left
        move = max(2.0, min(step_px, abs(remaining)))
        move = move if remaining >= 0 else -move
        await page.mouse.move(float(sx + move), float(sy + random.uniform(-1.0, 1.0)))
        await asyncio.sleep(random.uniform(0.03, 0.08))
        sx += move
        last_left = current_left

    await page.mouse.up()
    current_left = await _get_puzzle_css_left(page)
    return current_left is not None and abs(current_left - target_left_img) <= tolerance


async def solve_slider(page) -> bool:
    try:
        info = await _wait_for_slider_ready(page, timeout=10.0)
        if not info or not info.get("slider"):
            log.warning("Slider DOM not found")
            return False

        max_rounds = 6
        offsets = (0, -12, 12, -24, 24)

        for round_idx in range(1, max_rounds + 1):
            slider = info["slider"]
            bg = info["sliderBg"]
            container = info.get("sliderContainer")

            log.info(
                "Slider round %d/%d: x=%.1f y=%.1f w=%s h=%s",
                round_idx, max_rounds, slider["x"], slider["y"], slider["w"], slider["h"]
            )

            sx = slider["x"] + slider["w"] / 2
            sy = slider["y"] + slider["h"] / 2
            track_w = (
                container["w"] if container and container.get("w", 0) > 10 else bg["w"] - slider["w"] - 16
            )

            bg_rect = info.get("bgImageRect") or info.get("sliderBg") or {}
            computed = await _compute_drag_candidates(page, info)
            contour_w = float(computed[0].get("current_contour_w", 0.0)) if computed else 0.0
            bg_movable_w = max(1.0, float(bg_rect.get("w", 0)) - max(contour_w, 0.0))
            track_ratio = float(track_w) / bg_movable_w if bg_movable_w > 0 else 1.0
            log.info(
                "round %d geometry: track_w=%.1f bg_movable_w=%.1f contour_w=%.1f track_ratio=%.3f",
                round_idx, track_w, bg_movable_w, contour_w, track_ratio
            )

            drags = []
            for item in computed:
                drag = float(item["drag"]) * track_ratio
                if 5 <= drag <= track_w + 20:
                    drag = min(max(drag, 10.0), float(track_w - 4))
                    item["drag"] = drag
                    if all(abs(drag - existing["drag"]) > 2.0 for existing in drags):
                        drags.append(item)

            if not drags:
                fallback = track_w * random.uniform(0.45, 0.75)
                drags = [{
                    "drag": min(max(fallback, 10.0), float(track_w - 4)),
                    "row_y1": None,
                    "row_y2": None,
                    "puzzle_box": None,
                    "target_box": None,
                }]
                log.info("Fallback drag round %d: %.1fpx", round_idx, drags[0]["drag"])

            attempts = []
            for base in drags:
                for offset in offsets:
                    candidate = min(max(base["drag"] + offset, 10.0), float(track_w - 4))
                    if all(abs(candidate - existing["drag"]) > 1.0 for existing in attempts):
                        attempts.append({
                            "drag": candidate,
                            "target_left_img": base.get("target_left_img"),
                            "row_y1": base["row_y1"],
                            "row_y2": base["row_y2"],
                            "puzzle_box": base["puzzle_box"],
                            "target_box": base["target_box"],
                        })

            for idx, attempt in enumerate(attempts, start=1):
                drag = float(attempt["drag"])
                log.info("Final drag round %d attempt %d/%d: %.1fpx", round_idx, idx, len(attempts), drag)
                # Debug overlay is intentionally disabled in normal runs.
                # It is visual-only and does not affect candidate selection or drag control.
                target_left_img = attempt.get("target_left_img")
                if target_left_img is not None:
                    solved = await _drag_until_target_left(page, sx, sy, float(target_left_img))
                    log.info(
                        "Closed-loop drag round %d attempt %d target_left=%.1f solved=%s",
                        round_idx,
                        idx,
                        float(target_left_img),
                        solved,
                    )
                else:
                    await _human_drag(page, sx, sy, drag)
                await asyncio.sleep(random.uniform(1.2, 2.0))

                after = await _get_slider_info(page)
                if not after or not after.get("slider"):
                    log.info("Slider gone — captcha solved")
                    return True

                title = await page.title()
                if "antibot" not in title.lower() and "captcha" not in title.lower():
                    log.info("Title changed: %r — solved", title)
                    return True

                log.info("Slider still present after round %d attempt %d, title=%r", round_idx, idx, title)
                info = after
                break

        return False
    except Exception as e:
        log.warning("solve_slider error: %s", e)
        return False
