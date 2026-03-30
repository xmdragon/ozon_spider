import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from display_screenshot import build_display_screenshot_path, save_display_screenshot


class FakeImage:
    def __init__(self):
        self.saved_to = None

    def save(self, path):
        self.saved_to = Path(path)


def test_build_display_screenshot_path_uses_display_slug(tmp_path):
    path = build_display_screenshot_path(
        tmp_path,
        ":99",
        now=datetime(2026, 3, 30, 12, 0, 1),
    )
    assert path == tmp_path / "display_99_20260330_120001.png"


def test_save_display_screenshot_saves_png_in_output_dir(tmp_path):
    image = FakeImage()

    path = save_display_screenshot(
        tmp_path,
        ":0",
        grabber=lambda: image,
        now=datetime(2026, 3, 30, 12, 0, 2),
    )

    assert path == tmp_path / "display_0_20260330_120002.png"
    assert image.saved_to == path
