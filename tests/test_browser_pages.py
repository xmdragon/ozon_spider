import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from browser_pages import ensure_single_page


class FakePage:
    def __init__(self, url: str, closed: bool = False):
        self.url = url
        self._closed = closed
        self.close_calls = 0

    def is_closed(self):
        return self._closed

    async def close(self):
        self._closed = True
        self.close_calls += 1


@pytest.mark.asyncio
async def test_ensure_single_page_keeps_first_non_blank_and_closes_others():
    blank = FakePage("about:blank")
    main = FakePage("https://seller.ozon.ru/app/dashboard/main")
    extra = FakePage("https://seller.ozon.ru/app/registration/signin")

    page = await ensure_single_page([blank, main, extra], lambda: None)

    assert page is main
    assert blank.close_calls == 1
    assert extra.close_calls == 1
    assert main.close_calls == 0


@pytest.mark.asyncio
async def test_ensure_single_page_creates_page_when_none_alive():
    created = FakePage("about:blank")

    async def create_page():
        return created

    page = await ensure_single_page([FakePage("", closed=True)], create_page)

    assert page is created
