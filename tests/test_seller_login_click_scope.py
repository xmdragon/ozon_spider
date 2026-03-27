import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seller_login import SellerSession


class ScopeFakePage:
    def __init__(self, url: str):
        self.url = url
        self.evaluate_calls = 0

    async def evaluate(self, _script: str):
        self.evaluate_calls += 1
        return "BUTTON:Войти"


@pytest.mark.asyncio
async def test_click_login_button_only_runs_on_seller_signin_page():
    session = SellerSession(
        email="seller@example.com",
        app_password="secret",
        client_id="123",
    )
    session._page = ScopeFakePage(
        "https://sso.ozon.ru/auth/ozonid?token=abc"
    )

    assert await session._click_login_button() is False
    assert session._page.evaluate_calls == 0
