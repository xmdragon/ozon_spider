"""
Entry point: start Xvfb + Chrome, run spider, self-repair loop.
Exits when SUCCESS_THRESHOLD consecutive SKUs succeed.
"""
import asyncio
import json
import logging
import random
import sys
import time
import os

import requests

from config import (
    CHROME_BIN, CDP_PORT, BROWSER_DISPLAY, BROWSER_USE_XVFB, apply_browser_display_env,
    SUCCESS_THRESHOLD, OUTPUT_FILE,
)
from chrome_launcher import start_xvfb, start_chrome, kill
from spider import run_spider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def chrome_is_running(port: int) -> bool:
    try:
        r = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _display_lock_paths(display: str) -> list[str]:
    display_id = display.strip()
    if not display_id.startswith(":"):
        return []
    display_num = display_id[1:].split(".", 1)[0]
    if not display_num.isdigit():
        return []
    return [f"/tmp/.X{display_num}-lock", f"/tmp/.X11-unix/X{display_num}"]


async def main():
    xvfb_proc = None
    chrome_proc = None
    all_results = []
    consecutive_successes = 0
    attempt = 0
    # SKUs from command-line args or env var
    if len(sys.argv) > 1:
        skus = sys.argv[1:]
    elif os.getenv("SKUS"):
        skus = os.getenv("SKUS").split(",")
    else:
        log.error("No SKUs provided. Usage: python3 run.py SKU1 SKU2 ... or set SKUS env var")
        sys.exit(1)
    max_attempts = 10

    while consecutive_successes < SUCCESS_THRESHOLD and attempt < max_attempts:
        attempt += 1
        log.info(f"=== Attempt {attempt}/{max_attempts} | successes so far: {consecutive_successes}/{SUCCESS_THRESHOLD} ===")

        # Kill stale Chrome/Xvfb if any
        if chrome_proc:
            kill(chrome_proc)
            chrome_proc = None
            time.sleep(1)
        if xvfb_proc:
            kill(xvfb_proc)
            xvfb_proc = None
            time.sleep(1)

        # Also kill any leftover chrome processes
        os.system("pkill -f 'google-chrome.*remote-debugging' 2>/dev/null; sleep 1")

        apply_browser_display_env()
        if BROWSER_USE_XVFB:
            try:
                for path in _display_lock_paths(BROWSER_DISPLAY):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                xvfb_proc = start_xvfb(BROWSER_DISPLAY)
                log.info(f"Using Xvfb virtual display {BROWSER_DISPLAY}")
            except FileNotFoundError:
                log.warning("Xvfb not found — falling back to existing DISPLAY")
                xvfb_proc = None
        else:
            log.info(f"Using native display {BROWSER_DISPLAY}")

        # Start Chrome
        try:
            chrome_proc = start_chrome(CHROME_BIN, CDP_PORT, BROWSER_DISPLAY)
        except RuntimeError as e:
            log.error(f"Chrome failed to start: {e}")
            wait = random.uniform(5, 10)
            log.info(f"Waiting {wait:.1f}s before retry...")
            time.sleep(wait)
            continue

        # Pick a fresh rotation of SKUs (shuffle each attempt)
        random.shuffle(skus)
        batch = skus[:min(5, len(skus))]  # test 5 per attempt
        log.info(f"Batch SKUs: {batch}")

        try:
            results = await run_spider(batch, CDP_PORT)
        except RuntimeError as e:
            log.error(f"Spider error: {e}")
            results = []
        except Exception as e:
            log.error(f"Unexpected error: {e}")
            results = []

        if results:
            all_results.extend(results)
            consecutive_successes += len(results)
            log.info(f"Got {len(results)} results this batch. Total successes: {consecutive_successes}")
            # Save incrementally
            with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
                json.dump(all_results, f, ensure_ascii=False, indent=2)
            log.info(f"Saved {len(all_results)} results to {OUTPUT_FILE}")
        else:
            log.warning("No results this batch. Restarting Chrome.")
            consecutive_successes = 0  # reset streak on total failure

        if consecutive_successes < SUCCESS_THRESHOLD:
            wait = random.uniform(8, 15)
            log.info(f"Waiting {wait:.1f}s before next attempt...")
            time.sleep(wait)

    # Cleanup
    if chrome_proc:
        kill(chrome_proc)
    if xvfb_proc:
        kill(xvfb_proc)

    if consecutive_successes >= SUCCESS_THRESHOLD:
        log.info(f"SUCCESS: reached {SUCCESS_THRESHOLD} consecutive results. Output: {OUTPUT_FILE}")
    else:
        log.error(f"FAILED: only got {consecutive_successes} successes after {attempt} attempts")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
