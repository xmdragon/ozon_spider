import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seller_login import SellerSession, SellerSessionManager, SellerSessionUnavailable


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


class PoolFakeSession:
    def __init__(self, email: str, responses, probe_results=None):
        self.email = email
        self._responses = list(responses)
        self._probe_results = list(probe_results or [])
        self._request_lock = asyncio.Lock()
        self.storage_state_file = Path(f"/tmp/{email}.json")
        self.closed = False
        self.shallow_ready = True

    def is_shallow_ready(self):
        return self.shallow_ready and not self.closed

    async def close(self):
        self.closed = True

    async def ping(self):
        if not self._responses:
            return self.email
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def fetch_variant_model_result(self, *_args, **_kwargs):
        return await self.ping()

    async def fetch_data_v3(self, *_args, **_kwargs):
        return await self.ping()

    async def probe_api(self):
        if not self._probe_results:
            return True
        item = self._probe_results.pop(0)
        if isinstance(item, Exception):
            raise item
        return bool(item)


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


@pytest.mark.asyncio
async def test_probe_api_treats_400_as_alive_session():
    session = SellerSession(
        email="seller@example.com",
        app_password="secret",
        client_id="123",
    )
    session._page_fetch = AsyncMock(return_value=(400, {"code": "bad_request"}))

    assert await session.probe_api() is True


@pytest.mark.asyncio
async def test_manager_multi_master_round_robin_selection():
    manager = SellerSessionManager([
        {"email": "a@example.com", "app_password": "x", "client_id": "1"},
        {"email": "b@example.com", "app_password": "y", "client_id": "2"},
    ])
    manager._persist_accounts_locked = lambda: None
    manager._sessions = [
        PoolFakeSession("a@example.com", ["from-a"]),
        PoolFakeSession("b@example.com", ["from-b"]),
    ]

    assert await manager.call_with_failover("ping") == "from-a"
    assert await manager.call_with_failover("ping") == "from-b"


@pytest.mark.asyncio
async def test_manager_multi_master_removes_failed_session_and_uses_next():
    manager = SellerSessionManager([
        {"email": "a@example.com", "app_password": "x", "client_id": "1"},
        {"email": "b@example.com", "app_password": "y", "client_id": "2"},
    ])
    manager._persist_accounts_locked = lambda: None
    manager._schedule_recovery = lambda: None
    failed = PoolFakeSession("a@example.com", [SellerSessionUnavailable("dead")])
    healthy = PoolFakeSession("b@example.com", ["from-b"])
    manager._sessions = [failed, healthy]

    assert await manager.call_with_failover("ping") == "from-b"
    assert failed.closed is True
    assert [session.email for session in manager._sessions] == ["b@example.com"]


@pytest.mark.asyncio
async def test_ensure_pool_does_not_probe_when_pool_is_full():
    manager = SellerSessionManager([
        {"email": "a@example.com", "app_password": "x", "client_id": "1"},
        {"email": "b@example.com", "app_password": "y", "client_id": "2"},
    ])
    manager._persist_accounts_locked = lambda: None
    manager._sessions = [
        PoolFakeSession("a@example.com", []),
        PoolFakeSession("b@example.com", []),
    ]
    manager._drop_dead_sessions_locked = AsyncMock()

    await manager.ensure_pool()

    manager._drop_dead_sessions_locked.assert_awaited_once()
    assert [session.email for session in manager._sessions] == ["a@example.com", "b@example.com"]


@pytest.mark.asyncio
async def test_manager_soft_request_failures_require_two_hits_before_removal():
    manager = SellerSessionManager([
        {"email": "a@example.com", "app_password": "x", "client_id": "1"},
        {"email": "b@example.com", "app_password": "y", "client_id": "2"},
    ])
    manager._persist_accounts_locked = lambda: None
    manager._schedule_recovery = lambda: None
    flaky = PoolFakeSession(
        "a@example.com",
        [
            {"sku": "1", "status": "request_failed", "dimensions": None, "error": "http_500"},
            {"sku": "1", "status": "request_failed", "dimensions": None, "error": "http_500"},
        ],
    )
    healthy = PoolFakeSession(
        "b@example.com",
        [
            {"sku": "1", "status": "ok", "dimensions": {"weight": 1}, "error": None},
            {"sku": "1", "status": "ok", "dimensions": {"weight": 1}, "error": None},
        ],
    )
    manager._sessions = [flaky, healthy]

    first = await manager.call_with_failover("fetch_variant_model_result", "1")
    assert first["status"] == "request_failed"
    assert [session.email for session in manager._sessions] == ["a@example.com", "b@example.com"]

    second = await manager.call_with_failover("fetch_variant_model_result", "1")
    assert second["status"] == "ok"

    third = await manager.call_with_failover("fetch_variant_model_result", "1")
    assert third["status"] == "ok"
    assert flaky.closed is True
    assert [session.email for session in manager._sessions] == ["b@example.com"]
