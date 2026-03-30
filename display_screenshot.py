import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PIL import ImageGrab

log = logging.getLogger(__name__)


def _display_slug(display: str) -> str:
    raw = str(display or "").strip()
    if not raw:
        return "display"
    slug = raw.replace(":", "").replace(".", "_").replace("/", "_")
    return slug or "display"


def build_display_screenshot_path(
    output_dir: Path,
    display: str,
    now: Optional[datetime] = None,
) -> Path:
    ts = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return output_dir / f"display_{_display_slug(display)}_{ts}.png"


def save_display_screenshot(
    output_dir: Path,
    display: str,
    grabber: Optional[Callable[[], object]] = None,
    now: Optional[datetime] = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    image = (grabber or ImageGrab.grab)()
    path = build_display_screenshot_path(output_dir, display, now=now)
    image.save(path)
    return path


async def run_display_screenshot_loop(
    output_dir: Path,
    display: str,
    interval_seconds: int,
) -> None:
    last_error = None
    while True:
        try:
            path = await asyncio.to_thread(
                save_display_screenshot,
                output_dir,
                display,
            )
            last_error = None
            log.info("display screenshot saved: %s", path)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            error_repr = f"{type(e).__name__}: {e}"
            if error_repr != last_error:
                log.warning("display screenshot failed for %s: %s", display, e)
                last_error = error_repr
        await asyncio.sleep(interval_seconds)
