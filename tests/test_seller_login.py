import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seller_login import SellerSession


class FakePage:
    def __init__(self, url: str):
        self.url = url
        self.login_clicks = 0

    async def evaluate(self, script: str):
        assert "const exactTexts = new Set(['登录', 'Войти'])" in script
        self.login_clicks += 1
        self.url = "https://seller.ozon.ru/app/dashboard/products"
        return "BUTTON:登录"

    async def wait_for_load_state(self, *_args, **_kwargs):
        return None

    async def query_selector(self, _selector: str):
        return None


class RestoreFakePage:
    def __init__(self, url: str):
        self.url = url

    async def goto(self, *_args, **_kwargs):
        raise Exception("Timeout 30000ms exceeded")

    async def wait_for_load_state(self, *_args, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_existing_authenticated_flow_clicks_login_button_from_signin():
    session = SellerSession(
        email="seller@example.com",
        app_password="secret",
        client_id="123",
    )
    session._page = FakePage(
        "https://seller.ozon.ru/app/registration/signin?locale=zh-Hans&__rr=1&abt_att=1"
    )
    session._click_next_button = AsyncMock(return_value=False)
    session._find_email_input = AsyncMock(return_value=None)

    assert await session._handle_existing_authenticated_flow(timeout=1) is True
    assert session._page.login_clicks == 1


@pytest.mark.asyncio
async def test_restore_session_keeps_existing_flow_after_dashboard_timeout(tmp_path):
    state_file = tmp_path / "seller_state.json"
    state_file.write_text('{"cookies":[{"name":"sid","value":"1"}]}', encoding="utf-8")

    session = SellerSession(
        email="seller@example.com",
        app_password="secret",
        client_id="123",
        storage_state_file=str(state_file),
    )
    session._context = type("Ctx", (), {"add_cookies": AsyncMock()})()
    session._page = RestoreFakePage(
        "https://seller.ozon.ru/app/registration/signin?abt_att=1"
    )
    session._wait_for_login_ready = AsyncMock(return_value="ready")
    session._handle_existing_authenticated_flow = AsyncMock(return_value=True)

    assert await session._restore_session() is True
    session._context.add_cookies.assert_awaited_once()
    session._wait_for_login_ready.assert_awaited_once()
    session._handle_existing_authenticated_flow.assert_awaited_once()
