"""
Ozon Seller 登录模块。

启动独立系统 Chrome（独立 user-data-dir，CDP 端口 9224），
与 spider Chrome（端口 9223）完全 profile 隔离。
登录后 session 常驻，供后续请求 search-variant-mode 使用。
"""
import asyncio
import json
import logging
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any

from chrome_launcher import start_chrome, kill
from config import ACCOUNT_JSON_PATH, CHROME_BIN, BROWSER_DISPLAY, apply_browser_display_env

log = logging.getLogger(__name__)

SELLER_CDP_PORT = 9224
SELLER_DASHBOARD = "https://seller.ozon.ru/app/dashboard"
SELLER_LOGIN_URL = "https://seller.ozon.ru/"
SELLER_LOGIN_READY_TIMEOUT = 60
SELLER_LOGIN_POLL_INTERVAL = 0.25
SELLER_LOGIN_CHALLENGE_POLL_INTERVAL = 0.5
SELLER_LOGIN_LOADSTATE_TIMEOUT_MS = 500
SELLER_VERIFICATION_INITIAL_WAIT_SECONDS = 5
SELLER_VERIFICATION_POLL_ATTEMPTS = 12
SELLER_VERIFICATION_POLL_INTERVAL_SECONDS = 5
SELLER_VERIFICATION_EMAIL_MAX_AGE_SECONDS = 60
SELLER_VERIFICATION_EMAIL_LOOKBACK_MINUTES = 10
SELLER_POST_CODE_FLOW_TIMEOUT_SECONDS = 60
SELLER_EXISTING_FLOW_TIMEOUT_SECONDS = 60
SELLER_FAILED_SESSION_HOLD_SECONDS = 30
SELLER_ACCOUNT_RECOVERY_INTERVAL_SECONDS = 300
SELLER_ACCOUNT_RETRY_COOLDOWN_SECONDS = 300
SELLER_LOGIN_PROGRESS_STALE_SECONDS = 900
SELLER_SESSION_SOFT_FAILURE_THRESHOLD = 2
# seller 尺寸查询状态：
# - ok: seller 查询成功，且解析到尺寸/重量
# - no_data: seller 查询成功，但没有命中记录或记录里没有尺寸字段
# - request_failed: seller 请求异常、非 200、空 payload 等请求侧失败
VARIANT_MODEL_STATUS_OK = "ok"
VARIANT_MODEL_STATUS_NO_DATA = "no_data"
VARIANT_MODEL_STATUS_REQUEST_FAILED = "request_failed"


def _now_local() -> datetime:
    return datetime.now().astimezone()


def _iso_now() -> str:
    return _now_local().isoformat(timespec="seconds")


def _parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).astimezone()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
        except Exception:
            return None
    return None


def _dt_to_iso(value: Optional[datetime]) -> Optional[str]:
    if not value:
        return None
    return value.astimezone().isoformat(timespec="seconds")


def _variant_model_result(
    sku: str,
    status: str,
    dimensions: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "sku": str(sku),
        "status": status,
        "dimensions": dimensions,
        "error": error,
    }


def _ensure_retry_cooldown(
    explicit_until: Optional[datetime],
    started_at: Optional[datetime] = None,
) -> datetime:
    baseline = started_at or _now_local()
    minimum_until = baseline + timedelta(seconds=SELLER_ACCOUNT_RETRY_COOLDOWN_SECONDS)
    if explicit_until and explicit_until > minimum_until:
        return explicit_until
    return minimum_until


class SellerSessionUnavailable(RuntimeError):
    """Raised when a seller session is no longer authorized or usable."""


class SellerSession:
    """
    管理 Ozon Seller 的独立浏览器 session。
    使用系统 Chrome + 独立 user-data-dir，CDP 端口 9224。
    登录后 browser/context/page 保持常驻。
    """

    def __init__(self, email: str, app_password: str, client_id: str,
                 storage_state_file: str = "seller_state.json",
                 cdp_port: int = SELLER_CDP_PORT,
                 profile_dir: Optional[str] = None):
        self.email = email
        self.app_password = app_password
        self.client_id = client_id
        self.storage_state_file = Path(storage_state_file)
        self.cdp_port = cdp_port
        self.profile_dir = Path(profile_dir) if profile_dir else Path("seller_profile_" + self.email.split("@")[0])
        self.login_failure_reason: Optional[str] = None
        self.cooldown_until: Optional[datetime] = None
        self._chrome_proc = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._request_lock = asyncio.Lock()

    @property
    def page(self):
        return self._page

    @staticmethod
    def _is_authenticated_seller_url(url: str) -> bool:
        return (
            "seller.ozon.ru/app" in url
            and not any(k in url for k in ("signin", "ozonid", "sso", "registration"))
        )

    async def _read_page_text(self) -> str:
        try:
            body = await self._page.text_content("body") or ""
        except Exception:
            body = ""
        return " ".join(body.split())

    async def _is_seller_challenge_page(self) -> bool:
        try:
            title = (await self._page.title() or "").strip().lower()
        except Exception:
            title = ""
        body = (await self._read_page_text()).lower()
        return (
            ("доступ ограничен" in title or "access denied" in title)
            or "пожалуйста, включите javascript для продолжения" in body
            or "please, enable javascript for continue" in body
            or "нам нужно убедиться, что вы не робот" in body
            or "we need to make sure that you are not a robot" in body
        )

    async def _has_login_surface(self) -> bool:
        selectors = [
            'button[type="submit"]:has-text("登录")',
            'button:has-text("Войти")',
            'button:has-text("по почте")',
            'button:has-text("邮箱")',
            'input[type="email"]',
            'input[name="email"]',
            'input[type="tel"]',
        ]
        for sel in selectors:
            try:
                if await self._page.query_selector(sel):
                    return True
            except Exception:
                pass
        body = await self._read_page_text()
        return (
            "Войти по почте" in body
            or "使用邮箱登录" in body
            or "下一步" in body
            or "Далее" in body
        )

    async def _click_login_button(self) -> bool:
        url = self._page.url if self._page else ""
        if "seller.ozon.ru/app/registration/signin" not in url:
            return False
        try:
            clicked = await self._page.evaluate("""
                () => {
                    const exactTexts = new Set(['登录', 'Войти']);
                    for (const el of document.querySelectorAll('button, [role="button"], a, input[type="submit"]')) {
                        const raw = el.tagName === 'INPUT'
                            ? (el.value || '')
                            : (el.textContent || '');
                        const text = raw.trim();
                        if (!exactTexts.has(text)) {
                            continue;
                        }
                        el.click();
                        return `${el.tagName}:${text}`;
                    }
                    return null;
                }
            """)
        except Exception as e:
            if self._is_authenticated_seller_url(self._page.url):
                return True
            if "navigating" in str(e).lower():
                try:
                    await self._page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                return True
            raise

        if not clicked:
            return False

        log.info("点击登录按钮: %s", clicked)
        try:
            await self._page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        await asyncio.sleep(SELLER_LOGIN_POLL_INTERVAL)
        return True

    async def _wait_for_login_ready(self, label: str, timeout: int = SELLER_LOGIN_READY_TIMEOUT) -> str:
        deadline = time.time() + timeout
        saw_challenge = False
        while time.time() < deadline:
            url = self._page.url
            if self._is_authenticated_seller_url(url):
                return "authenticated"
            if await self._is_seller_challenge_page():
                if not saw_challenge:
                    saw_challenge = True
                    log.info("%s 检测到 seller anti-bot 过渡页，等待其自动跳转", label)
                await asyncio.sleep(SELLER_LOGIN_CHALLENGE_POLL_INTERVAL)
                continue
            if await self._has_login_surface():
                return "ready"
            try:
                await self._page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=SELLER_LOGIN_LOADSTATE_TIMEOUT_MS,
                )
            except Exception:
                pass
            await asyncio.sleep(SELLER_LOGIN_POLL_INTERVAL)
        return "timeout"

    async def _parse_verification_cooldown(self) -> Optional[datetime]:
        body = (await self._read_page_text()).lower()
        seconds = 0

        minute_match = re.search(r"(\d+)\s*(мин|мину|minute|min)", body)
        second_match = re.search(r"(\d+)\s*(сек|секунд|second|sec|秒)", body)

        if minute_match:
            seconds += int(minute_match.group(1)) * 60
        if second_match:
            seconds += int(second_match.group(1))

        if not seconds:
            return None
        return _now_local() + timedelta(seconds=seconds)

    async def _find_email_input(self):
        email_input = await self._page.query_selector(
            'input[type="email"], input[name="email"], '
            'input[placeholder*="почт"], input[placeholder*="email"]'
        )
        if email_input:
            return email_input

        inputs = await self._page.query_selector_all('input')
        for inp in inputs:
            t = await inp.get_attribute('type') or 'text'
            autocomplete = (await inp.get_attribute('autocomplete') or '').lower()
            inputmode = (await inp.get_attribute('inputmode') or '').lower()
            name = (await inp.get_attribute('name') or '').lower()
            placeholder = (await inp.get_attribute('placeholder') or '').lower()
            if autocomplete == "one-time-code":
                continue
            if inputmode == "numeric":
                continue
            if "code" in name or "код" in placeholder:
                continue
            if t in ('text', 'email', ''):
                return inp
        return None

    async def _has_next_button(self) -> bool:
        try:
            return bool(await self._page.evaluate("""
                () => {
                    const hasExact = (nodes) => Array.from(nodes).some((el) => {
                        const t = (el.textContent || '').trim();
                        return t === '下一步' || t === 'Далее' || t.includes('下一步') || t.includes('Далее');
                    });
                    return hasExact(document.querySelectorAll('button'))
                        || hasExact(document.querySelectorAll('span, div, a'));
                }
            """))
        except Exception:
            return False

    async def _find_code_input(self):
        selectors = [
            'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"]',
            'input[type="text"]',
        ]
        for sel in selectors:
            try:
                locator = self._page.locator(sel).first
                if await locator.count() > 0:
                    return locator
            except Exception:
                continue
        return None

    async def _fill_email_input(self, email: str) -> bool:
        selectors = [
            'input[type="email"]',
            'input[name="email"]',
            'input[placeholder*="почт"]',
            'input[placeholder*="email"]',
        ]
        for _ in range(20):
            for sel in selectors:
                try:
                    locator = self._page.locator(sel).first
                    if await locator.count() == 0:
                        continue
                    await locator.fill(email)
                    return True
                except Exception as e:
                    if "not attached" not in str(e).lower():
                        continue
            try:
                inputs = await self._page.query_selector_all("input")
                for inp in inputs:
                    try:
                        t = await inp.get_attribute("type") or "text"
                        if t not in ("text", "email", ""):
                            continue
                        await inp.fill(email)
                        return True
                    except Exception:
                        continue
            except Exception:
                pass
            await asyncio.sleep(0.25)
        return False

    async def _fill_verification_code(self, code: str) -> bool:
        for _ in range(20):
            try:
                locator = await self._find_code_input()
                if locator:
                    await locator.fill(code)
                    return True
            except Exception:
                pass
            try:
                await self._page.evaluate(
                    '([val]) => { const el = document.querySelector(\'input[autocomplete="one-time-code"], input[inputmode="numeric"], input[type="text"]\'); '
                    'if(el){ el.value=val; el.dispatchEvent(new Event("input",{bubbles:true})); el.dispatchEvent(new Event("change",{bubbles:true})); return true; } return false; }',
                    [code],
                )
                return True
            except Exception:
                pass
            await asyncio.sleep(0.25)
        return False

    async def _wait_after_click_next(self, previous_url: str, timeout: int = 15) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            url = self._page.url
            if self._is_authenticated_seller_url(url):
                return "authenticated"
            if url != previous_url:
                return "url_changed"
            if not await self._has_next_button():
                return "dom_changed"
            try:
                await self._page.wait_for_load_state("domcontentloaded", timeout=SELLER_LOGIN_LOADSTATE_TIMEOUT_MS)
            except Exception:
                pass
            await asyncio.sleep(SELLER_LOGIN_POLL_INTERVAL)
        return "timeout"

    async def _wait_for_post_code_ready(self, timeout: int = SELLER_POST_CODE_FLOW_TIMEOUT_SECONDS) -> str:
        deadline = time.time() + timeout
        saw_transition = False
        while time.time() < deadline:
            url = self._page.url
            if self._is_authenticated_seller_url(url):
                return "authenticated"
            if "auth/2fa" in url or "otp" in url:
                saw_transition = True
                try:
                    await self._page.wait_for_load_state("domcontentloaded", timeout=SELLER_LOGIN_LOADSTATE_TIMEOUT_MS)
                except Exception:
                    pass
                await asyncio.sleep(SELLER_LOGIN_CHALLENGE_POLL_INTERVAL)
                continue
            if await self._has_next_button():
                return "next"
            code_input = await self._find_code_input()
            if code_input:
                saw_transition = True
                try:
                    await self._page.wait_for_load_state("domcontentloaded", timeout=SELLER_LOGIN_LOADSTATE_TIMEOUT_MS)
                except Exception:
                    pass
                await asyncio.sleep(SELLER_LOGIN_POLL_INTERVAL)
                continue
            if await self._find_email_input():
                if saw_transition:
                    return "email_input"
                try:
                    await self._page.wait_for_load_state("domcontentloaded", timeout=SELLER_LOGIN_LOADSTATE_TIMEOUT_MS)
                except Exception:
                    pass
                await asyncio.sleep(SELLER_LOGIN_POLL_INTERVAL)
                continue
            try:
                await self._page.wait_for_load_state("domcontentloaded", timeout=SELLER_LOGIN_LOADSTATE_TIMEOUT_MS)
            except Exception:
                pass
            await asyncio.sleep(SELLER_LOGIN_POLL_INTERVAL)
        return "timeout"

    async def _click_next_button(self) -> bool:
        try:
            previous_url = self._page.url
            content = await self._page.content()
        except Exception as e:
            if self._is_authenticated_seller_url(self._page.url):
                return False
            if "navigating" in str(e).lower():
                try:
                    await self._page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                return False
            raise
        if "下一步" not in content and "Далее" not in content:
            return False

        try:
            clicked = await self._page.evaluate("""
                () => {
                    const btns = document.querySelectorAll('button');
                    for (const el of btns) {
                        const t = (el.textContent || '').trim();
                        if (t === '下一步' || t === 'Далее' || t.includes('下一步') || t.includes('Далее')) {
                            el.click();
                            return 'BUTTON:' + (el.className || '');
                        }
                    }
                    const all = document.querySelectorAll('span, div, a');
                    let smallest = null, smallestArea = Infinity;
                    for (const el of all) {
                        const t = (el.textContent || '').trim();
                        if (t === '下一步' || t === 'Далее') {
                            const r = el.getBoundingClientRect();
                            const area = r.width * r.height;
                            if (area > 0 && area < smallestArea) {
                                smallest = el;
                                smallestArea = area;
                            }
                        }
                    }
                    if (smallest) {
                        smallest.click();
                        return smallest.tagName + ':' + (smallest.className || '');
                    }
                    return null;
                }
            """)
        except Exception as e:
            if self._is_authenticated_seller_url(self._page.url):
                return False
            if "navigating" in str(e).lower():
                try:
                    await self._page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                return False
            raise
        if not clicked:
            return False

        log.info("点击下一步: %s", clicked)
        result = await self._wait_after_click_next(previous_url)
        log.info("点击下一步后状态: %s | URL: %s", result, self._page.url)
        return True

    async def _handle_existing_authenticated_flow(self, timeout: int = SELLER_EXISTING_FLOW_TIMEOUT_SECONDS) -> bool:
        """处理 2FA/回落 signin 后只需点“登录/下一步”的已认证流程。"""
        if not self._page:
            return False

        saw_2fa = False
        deadline = time.time() + timeout
        while time.time() < deadline:
            url = self._page.url
            if self._is_authenticated_seller_url(url):
                return True

            if "auth/2fa" in url:
                if not saw_2fa:
                    saw_2fa = True
                    log.info("检测到 2FA 过渡页，等待其自动跳转")
                await asyncio.sleep(SELLER_LOGIN_CHALLENGE_POLL_INTERVAL)
                continue

            if await self._click_next_button():
                if self._is_authenticated_seller_url(self._page.url):
                    return True
                continue

            if await self._click_login_button():
                if self._is_authenticated_seller_url(self._page.url):
                    return True
                continue

            for sel in [
                'button:has-text("по почте")',
                'button:has-text("邮箱")',
                'text="Войти по почте"',
                'text="使用邮箱登录"',
                'input[type="tel"]',
            ]:
                if await self._page.query_selector(sel):
                    return False

            email_input = await self._find_email_input()
            if email_input:
                return False

            await asyncio.sleep(SELLER_LOGIN_POLL_INTERVAL)

        return self._is_authenticated_seller_url(self._page.url)

    async def start(self, allow_login: bool = True) -> bool:
        """
        启动 seller Chrome 并建立 session。
        先尝试恢复已有 session（storage_state），失败则重新登录。
        """
        from playwright.async_api import async_playwright

        # 每个账号使用独立的 Chrome profile（按 email 区分）
        apply_browser_display_env()
        seller_profile = self.profile_dir
        seller_profile.mkdir(exist_ok=True)
        self._chrome_proc = start_chrome(CHROME_BIN, self.cdp_port, BROWSER_DISPLAY,
                                         user_data_dir=str(seller_profile.absolute()))

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self.cdp_port}"
        )
        self._context = self._browser.contexts[0] if self._browser.contexts else \
            await self._browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow")
        pages = [p for p in self._context.pages if not p.is_closed()]
        blank_pages = [p for p in pages if p.url in ("", "about:blank")]
        if blank_pages:
            self._page = blank_pages[0]
            for extra in blank_pages[1:]:
                try:
                    await extra.close()
                except Exception:
                    pass
        else:
            self._page = await self._context.new_page()

        # 尝试恢复已有 session
        if self.storage_state_file.exists():
            log.info("尝试恢复已有 seller session...")
            if await self._restore_session():
                log.info("✓ Seller session 恢复成功")
                return True
            log.warning("Session 已失效，重新登录")
            if not allow_login:
                return False
        elif not allow_login:
            log.info("未找到可恢复的 seller session: %s", self.storage_state_file)
            return False

        return await self._do_login()

    async def _restore_session(self) -> bool:
        """注入已保存的 cookies 并验证 session。"""
        try:
            with open(self.storage_state_file, encoding="utf-8") as f:
                state = json.load(f)
            cookies = state.get("cookies", [])
            if not cookies:
                return False
            await self._context.add_cookies(cookies)
            goto_error = None
            try:
                await self._page.goto(SELLER_DASHBOARD, timeout=30000, wait_until="domcontentloaded")
            except Exception as e:
                goto_error = e
                log.warning("恢复 session 跳转 dashboard 异常，继续检查当前页面: %s", e)
            try:
                await self._page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            await asyncio.sleep(2)
            ready_state = await self._wait_for_login_ready("恢复 session 后", timeout=30)
            url = self._page.url
            if goto_error:
                log.info("恢复 session 后状态: %s | URL: %s | goto_error: %s", ready_state, url, goto_error)
            else:
                log.info("恢复 session 后状态: %s | URL: %s", ready_state, url)
            if ready_state == "authenticated":
                log.info("session 验证通过")
                return True
            if ready_state == "ready" or any(k in url for k in ["signin", "ozonid", "sso"]):
                if await self._handle_existing_authenticated_flow():
                    log.info("恢复 session 后通过已认证流程进入 seller")
                    return True
                return False
            return False
        except Exception as e:
            log.warning("恢复 session 失败: %s", e)
            return False

    async def _do_login(self) -> bool:
        """执行完整邮箱登录流程。"""
        from email_service import EmailService

        try:
            self.login_failure_reason = None
            self.cooldown_until = None
            # 直接访问 seller 登录页（跳过首页 → ozonid 跳转，避免 antibot）
            signin_url = "https://seller.ozon.ru/app/registration/signin?locale=zh-Hans"
            log.info("访问登录页: %s", signin_url)
            await self._page.goto(signin_url, timeout=60000, wait_until="domcontentloaded")
            try:
                await self._page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                await asyncio.sleep(3)

            log.info("登录页 URL: %s", self._page.url)
            ready_state = await self._wait_for_login_ready("进入登录页后", timeout=60)
            log.info("登录页就绪状态: %s | URL: %s", ready_state, self._page.url)
            if ready_state == "authenticated":
                return True
            if ready_state == "timeout":
                self.login_failure_reason = "signin_not_ready_timeout"
                log.warning("seller 登录页在等待 anti-bot 跳转后仍未就绪，URL: %s", self._page.url)
                return False

            # 步骤1: 点击「登录」按钮
            if await self._click_login_button():
                post_click_state = await self._wait_for_login_ready("点击登录后", timeout=60)
                log.info("点击登录后状态: %s | URL: %s", post_click_state, self._page.url)
                if post_click_state == "authenticated":
                    return True
                if post_click_state == "timeout":
                    self.login_failure_reason = "post_signin_not_ready_timeout"
                    log.warning("seller 点击登录后仍卡在过渡页，URL: %s", self._page.url)
                    return False
            log.info("点击登录后 URL: %s", self._page.url)

            if await self._handle_existing_authenticated_flow():
                log.info("检测到已认证流程，无需重新收验证码")
            else:
                pre_sso_state = await self._wait_for_login_ready("进入邮箱登录前", timeout=60)
                log.info("邮箱登录前状态: %s | URL: %s", pre_sso_state, self._page.url)
                if pre_sso_state == "authenticated":
                    return True
                if pre_sso_state == "timeout":
                    self.login_failure_reason = "email_entry_not_ready_timeout"
                    log.warning("seller 邮箱登录入口在等待 anti-bot 跳转后仍未出现，URL: %s", self._page.url)
                    return False
                # 等待 SSO 页面完全渲染（等「Войти по почте」出现）
                for sel in ['button:has-text("по почте")', 'button:has-text("邮箱")', 'input[type="tel"]']:
                    try:
                        await self._page.wait_for_selector(sel, timeout=15000)
                        log.info("SSO 页面就绪 (selector: %s)", sel)
                        break
                    except Exception:
                        continue

                # 点击「邮箱登录」/「Войти по почте」
                for _ in range(10):
                    clicked = False
                    for sel in [
                        'button:has-text("по почте")', 'button:has-text("邮箱")',
                        'text="Войти по почте"', 'a:has-text("Войти по почте")',
                        'text="使用邮箱登录"', '[data-testid*="email"]',
                    ]:
                        btn = await self._page.query_selector(sel)
                        if btn:
                            await btn.evaluate('el => el.click()')
                            log.info("点击邮箱登录: %s", sel)
                            clicked = True
                            break
                    if clicked:
                        break
                    await asyncio.sleep(SELLER_LOGIN_POLL_INTERVAL)

                post_email_mode_state = await self._wait_for_login_ready("点击邮箱登录后", timeout=60)
                log.info("点击邮箱登录后状态: %s | URL: %s", post_email_mode_state, self._page.url)
                if post_email_mode_state == "authenticated":
                    return True
                if post_email_mode_state == "timeout":
                    self.login_failure_reason = "post_email_mode_not_ready_timeout"
                    log.warning("seller 点击邮箱登录后页面仍未就绪，URL: %s", self._page.url)
                    return False
                log.info("当前 URL: %s", self._page.url)

                # 填邮箱
                email_input = await self._find_email_input()
                if not email_input:
                    if await self._handle_existing_authenticated_flow():
                        log.info("回落 signin 后通过下一步进入已认证流程")
                    else:
                        self.login_failure_reason = "email_input_missing"
                        log.error("未找到邮箱输入框，URL: %s", self._page.url)
                        return False
                else:
                    if not await self._fill_email_input(self.email):
                        self.login_failure_reason = "email_input_fill_failed"
                        log.error("邮箱输入框存在，但填充失败，URL: %s", self._page.url)
                        return False
                    log.info("邮箱已输入: %s", self.email)
                    email_svc = EmailService(self.email, self.app_password)
                    code = None

                    # 提交
                    send_btn = await self._page.query_selector(
                        'button:has-text("Отправить"), button:has-text("Продолжить"), button[type="submit"]'
                    )
                    if send_btn:
                        await send_btn.click()
                    else:
                        await self._page.keyboard.press("Enter")
                    send_requested_at = _iso_now()
                    send_requested_monotonic = time.monotonic()
                    log.info("发送验证码请求: email=%s at=%s", self.email, send_requested_at)
                    await asyncio.sleep(SELLER_VERIFICATION_INITIAL_WAIT_SECONDS)
                    log.info(
                        "发送验证码请求后等待 %.1fs，开始首次拉取邮件",
                        time.monotonic() - send_requested_monotonic,
                    )

                    # 等待验证码（只取 ID > last_id 的新邮件）
                    # 邮件投递可能明显晚于页面发码动作，这里保守拉长观察窗口。
                    log.info("等待验证码邮件...")
                    with email_svc:
                        email_svc.connect_imap()
                        for attempt in range(SELLER_VERIFICATION_POLL_ATTEMPTS):
                            log.info(
                                "检查验证码邮件，第 %d/%d 轮（距发送 %.1fs）",
                                attempt + 1,
                                SELLER_VERIFICATION_POLL_ATTEMPTS,
                                time.monotonic() - send_requested_monotonic,
                            )
                            latest_mail = email_svc.find_latest_ozon_verification_email(
                                max_age_seconds=SELLER_VERIFICATION_EMAIL_MAX_AGE_SECONDS,
                                check_spam=True,
                                minutes=SELLER_VERIFICATION_EMAIL_LOOKBACK_MINUTES,
                                limit=20,
                            )
                            if latest_mail:
                                code = str(latest_mail["code"])
                                log.info(
                                    "收到最新验证码: %s (folder=%s, ID=%s, date=%s)",
                                    code,
                                    latest_mail["folder"],
                                    latest_mail["id"],
                                    latest_mail["date"],
                                )
                            if code:
                                break
                            if attempt < SELLER_VERIFICATION_POLL_ATTEMPTS - 1:
                                await asyncio.sleep(SELLER_VERIFICATION_POLL_INTERVAL_SECONDS)

                    if not code:
                        self.login_failure_reason = "verification_code_timeout"
                        self.cooldown_until = await self._parse_verification_cooldown() or (_now_local() + timedelta(minutes=30))
                        log.error("未收到验证码")
                        return False
                    log.info("收到验证码")

                    # 填入验证码（重新查询避免旧句柄过期）
                    if not await self._fill_verification_code(code):
                        self.login_failure_reason = "verification_code_fill_failed"
                        log.error("验证码输入框存在，但填充失败，URL: %s", self._page.url)
                        return False
                    log.info("验证码已填入")

                    post_code_state = await self._wait_for_post_code_ready(timeout=30)
                    log.info("验证码后状态: %s | URL: %s", post_code_state, self._page.url)

                    # 检测并点击「下一步」
                    if post_code_state != "authenticated":
                        for _ in range(10):
                            if await self._click_next_button():
                                break
                            if self._is_authenticated_seller_url(self._page.url):
                                break
                            await asyncio.sleep(SELLER_LOGIN_POLL_INTERVAL)

                    if not self._is_authenticated_seller_url(self._page.url):
                        if await self._handle_existing_authenticated_flow():
                            log.info("验证码后通过认证过渡流程进入 seller")

            log.info("下一步后 URL: %s", self._page.url)

            # 如果还在 signin，再点一次登录按钮
            if "signin" in self._page.url or "registration" in self._page.url:
                if await self._click_login_button():
                    post_click_state = await self._wait_for_login_ready("再次点击登录后", timeout=30)
                    log.info("再次点击登录后状态: %s | URL: %s", post_click_state, self._page.url)

            log.info("最终 URL: %s", self._page.url)

            # 验证：不在认证页即为成功
            final_url = self._page.url
            if any(k in final_url for k in ("signin", "ozonid", "sso", "registration")):
                self.login_failure_reason = "still_on_auth_page"
                log.warning("登录后仍在认证页，登录失败，URL: %s", final_url)
                return False

            # 保存 cookies
            log.info("✓ 登录成功，保存 cookies")
            state = {"cookies": await self._context.cookies()}
            with open(self.storage_state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            log.info("Cookies 已保存到 %s", self.storage_state_file)
            return True

        except Exception as e:
            self.login_failure_reason = f"exception:{type(e).__name__}"
            log.error("登录异常: %s", e, exc_info=True)
            return False

    # Dimension attribute keys from seller API
    _DIMENSION_ATTR_KEYS = {
        "4497": "weight",  # 重量(g)
        "9454": "depth",   # 深度/长度(mm)
        "9455": "width",   # 宽度(mm)
        "9456": "height",  # 高度(mm)
    }

    async def _page_fetch(self, url: str, payload: dict, extra_headers: dict = None) -> tuple:
        """在 seller page 上下文中发 POST fetch 请求，返回 (status, data)。"""
        if not self._page:
            raise SellerSessionUnavailable("seller page is not initialized")
        if any(k in self._page.url for k in ("signin", "ozonid", "sso", "registration")):
            raise SellerSessionUnavailable(f"seller page is on auth URL: {self._page.url}")

        headers = {
            "Content-Type": "application/json",
            "X-O3-Company-Id": self.client_id,
            "X-O3-App-Name": "seller-ui",
            "X-O3-Language": "zh-Hans",
            "X-O3-Page-Type": "products-other",
        }
        if extra_headers:
            headers.update(extra_headers)
        result = await self._page.evaluate("""
            async ([url, payload, headers]) => {
                const r = await fetch(url, {
                    method: 'POST',
                    headers: headers,
                    body: JSON.stringify(payload),
                    credentials: 'include',
                });
                let data = null;
                try { data = await r.json(); } catch(e) {}
                return {status: r.status, data: data};
            }
        """, [url, payload, headers])
        return result.get('status', 0), result.get('data')

    async def probe_api(self) -> bool:
        """用轻量 seller API 检查当前 session 是否仍然授权可用。"""
        try:
            status, _ = await self._page_fetch(
                "https://seller.ozon.ru/api/v1/search-variant-model",
                {"limit": "1", "name": "1"},
            )
            return status not in (0, 401, 403)
        except Exception as e:
            log.warning("seller probe failed for %s: %s", self.email, e)
            return False

    def is_shallow_ready(self) -> bool:
        if not self._page:
            return False
        try:
            if self._page.is_closed():
                return False
        except Exception:
            return False
        return not any(k in self._page.url for k in ("signin", "ozonid", "sso", "registration"))

    async def fetch_variant_model_result(self, sku: str) -> Dict[str, Any]:
        """
        请求 search-variant-model 获取单个 SKU 的尺寸/重量状态。

        返回:
        - {"sku", "status", "dimensions", "error"}

        其中 status 含义为:
        - ok: seller 查询成功，且拿到了尺寸/重量
        - no_data: seller 查询成功，但这条 SKU 没有尺寸数据
        - request_failed: seller 请求失败或返回异常 payload

        注意:
        - seller 会话失效/未授权不走 status，直接抛 SellerSessionUnavailable
        """
        try:
            status, data = await self._page_fetch(
                "https://seller.ozon.ru/api/v1/search-variant-model",
                {"limit": "10", "name": str(sku)},
            )
            if status in (401, 403):
                raise SellerSessionUnavailable(f"variant-model unauthorized for {self.email}: status={status}")
            if status != 200:
                log.warning("variant-model SKU %s status=%d", sku, status)
                return _variant_model_result(
                    sku,
                    VARIANT_MODEL_STATUS_REQUEST_FAILED,
                    error=f"http_{status}",
                )
            if not data:
                log.warning("variant-model SKU %s empty payload", sku)
                return _variant_model_result(
                    sku,
                    VARIANT_MODEL_STATUS_REQUEST_FAILED,
                    error="empty_payload",
                )
            items = data.get("items", [])
            if not items:
                log.info("variant-model SKU %s returned 0 items", sku)
                return _variant_model_result(sku, VARIANT_MODEL_STATUS_NO_DATA)
            attrs = items[0].get("attributes", [])
            dimensions = {}
            for attr in attrs:
                key = str(attr.get("key", ""))
                if key in self._DIMENSION_ATTR_KEYS:
                    try:
                        dimensions[self._DIMENSION_ATTR_KEYS[key]] = float(attr["value"])
                    except (ValueError, KeyError):
                        pass
            if not dimensions:
                log.info("variant-model SKU %s returned item without dimensions", sku)
                return _variant_model_result(sku, VARIANT_MODEL_STATUS_NO_DATA)
            log.info("SKU %s dimensions: %s", sku, dimensions)
            return _variant_model_result(
                sku,
                VARIANT_MODEL_STATUS_OK,
                dimensions=dimensions,
            )
        except SellerSessionUnavailable:
            raise
        except Exception as e:
            msg = str(e)
            if "closed" in msg.lower() or "target page" in msg.lower():
                raise SellerSessionUnavailable(f"variant-model page closed for {self.email}: {e}") from e
            log.warning("fetch_variant_model error: %s", e)
            return _variant_model_result(
                sku,
                VARIANT_MODEL_STATUS_REQUEST_FAILED,
                error=type(e).__name__,
            )

    async def fetch_variant_model(self, sku: str) -> Optional[Dict[str, Any]]:
        result = await self.fetch_variant_model_result(sku)
        if result["status"] != VARIANT_MODEL_STATUS_OK:
            return None
        return result["dimensions"]

    async def fetch_data_v3(self, skus: list) -> Optional[Dict[str, Any]]:
        """
        请求 data/v3 获取批量 SKU 的销售分析数据。
        返回原始 API 响应 data 字段。
        """
        try:
            status, data = await self._page_fetch(
                "https://seller.ozon.ru/api/site/seller-analytics/what_to_sell/data/v3",
                {
                    "filter": {
                        "name": " ".join(str(s) for s in skus),
                        "period": "monthly",
                        "stock": "any_stock",
                    },
                    "sort": {"col": "gmv", "asc": False},
                    "limit": 50,
                    "offset": 0,
                },
                extra_headers={"X-O3-Page-Type": "product-analytics"},
            )
            if status in (401, 403):
                raise SellerSessionUnavailable(f"data/v3 unauthorized for {self.email}: status={status}")
            if status != 200:
                log.warning("data/v3 status=%d", status)
                return None
            log.info("data/v3 返回 %d items", len((data or {}).get('items', [])))
            return data
        except SellerSessionUnavailable:
            raise
        except Exception as e:
            msg = str(e)
            if "closed" in msg.lower() or "target page" in msg.lower():
                raise SellerSessionUnavailable(f"data/v3 page closed for {self.email}: {e}") from e
            log.warning("fetch_data_v3 error: %s", e)
            return None

    async def fetch_variant_dimensions(self, sku: str) -> Optional[Dict[str, Any]]:
        """向后兼容别名。"""
        return await self.fetch_variant_model(sku)

    async def close(self):
        """关闭 seller 浏览器，释放资源。"""
        try:
            if self._page:
                await self._page.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        if self._playwright:
            await self._playwright.stop()
        if self._chrome_proc:
            kill(self._chrome_proc)

    def purge_session_artifacts(self, reason: str) -> None:
        """删除失效 seller session 的持久化状态，避免重复恢复脏 profile。"""
        try:
            self.storage_state_file.unlink(missing_ok=True)
        except Exception as e:
            log.warning("清理 seller state 失败 %s (%s): %s", self.storage_state_file, reason, e)
        try:
            if self.profile_dir.exists():
                shutil.rmtree(self.profile_dir, ignore_errors=True)
        except Exception as e:
            log.warning("清理 seller profile 失败 %s (%s): %s", self.profile_dir, reason, e)

    async def hold_before_close(self, reason: str, seconds: int = SELLER_FAILED_SESSION_HOLD_SECONDS) -> None:
        if seconds <= 0:
            return
        log.warning(
            "seller session failure hold: email=%s reason=%s url=%s hold=%ss",
            self.email,
            reason,
            self._page.url if self._page else "",
            seconds,
        )
        await asyncio.sleep(seconds)


async def get_seller_session(
    email: str, app_password: str, client_id: str,
    storage_state_file: str = "seller_state.json",
    allow_login: bool = True,
    cdp_port: int = SELLER_CDP_PORT,
    profile_dir: Optional[str] = None,
) -> Optional[SellerSession]:
    """
    创建并启动 SellerSession。返回就绪的 session，失败返回 None。
    """
    session = SellerSession(
        email,
        app_password,
        client_id,
        storage_state_file,
        cdp_port=cdp_port,
        profile_dir=profile_dir,
    )
    ok = await session.start(allow_login=allow_login)
    if ok:
        return session
    await session.close()
    return None


def _account_storage_state(email: str) -> str:
    return f"seller_state_{email.split('@')[0]}.json"


def _account_profile_dir(email: str) -> str:
    return f"seller_profile_{email.split('@')[0]}"


def _account_cdp_port(index: int) -> int:
    return SELLER_CDP_PORT + index


def _normalize_account(acct: dict, index: int) -> dict:
    email = str(acct["email"]).strip()
    normalized = dict(acct)
    normalized["email"] = email
    normalized["app_password"] = str(acct["app_password"]).strip()
    normalized["client_id"] = str(acct["client_id"]).strip()
    normalized["state_file"] = str(acct.get("state_file") or _account_storage_state(email))
    normalized["profile_dir"] = str(acct.get("profile_dir") or _account_profile_dir(email))
    normalized["cdp_port"] = int(acct.get("cdp_port") or _account_cdp_port(index))
    normalized["status"] = str(acct.get("status") or "unknown")
    normalized["last_state_ok_at"] = acct.get("last_state_ok_at")
    normalized["last_login_ok_at"] = acct.get("last_login_ok_at")
    normalized["last_login_error"] = str(acct.get("last_login_error") or "")
    normalized["cooldown_until"] = acct.get("cooldown_until")
    normalized["login_in_progress"] = bool(acct.get("login_in_progress", False))
    normalized["last_login_started_at"] = acct.get("last_login_started_at")
    return normalized


def _serialize_account(acct: dict) -> dict:
    return {
        "email": acct["email"],
        "app_password": acct["app_password"],
        "client_id": acct["client_id"],
        "state_file": acct["state_file"],
        "profile_dir": acct["profile_dir"],
        "status": acct.get("status") or "unknown",
        "last_state_ok_at": acct.get("last_state_ok_at"),
        "last_login_ok_at": acct.get("last_login_ok_at"),
        "last_login_error": acct.get("last_login_error") or "",
        "cooldown_until": acct.get("cooldown_until"),
        "login_in_progress": bool(acct.get("login_in_progress", False)),
        "last_login_started_at": acct.get("last_login_started_at"),
    }


def _account_state_score(acct: dict) -> float:
    state_path = Path(acct["state_file"])
    candidates = []
    if state_path.exists():
        try:
            candidates.append(state_path.stat().st_mtime)
        except Exception:
            pass
    for key in ("last_state_ok_at", "last_login_ok_at"):
        dt = _parse_time(acct.get(key))
        if dt:
            candidates.append(dt.timestamp())
    return max(candidates) if candidates else 0.0


def _cooldown_active(acct: dict) -> bool:
    dt = _parse_time(acct.get("cooldown_until"))
    return bool(dt and dt > _now_local())


class SellerSessionManager:
    """Manage a multi-master seller session pool with background recovery."""

    def __init__(self, accounts: list):
        self._account_file = ACCOUNT_JSON_PATH
        self._accounts = [_normalize_account(acct, idx) for idx, acct in enumerate(accounts)]
        self._boot_time = _now_local()
        self._sessions: list[SellerSession] = []
        self._session_inflight: Dict[str, int] = {}
        self._session_soft_failures: Dict[str, int] = {}
        self._rr_cursor = 0
        self._lock = asyncio.Lock()
        self._recovery_task: Optional[asyncio.Task] = None
        self._recovery_wakeup = asyncio.Event()
        self._stopped = False
        self._last_failure: Optional[Dict[str, Any]] = None
        self._last_recovery_error: Optional[str] = None

    @property
    def active_session(self) -> Optional[SellerSession]:
        return self._sessions[0] if self._sessions else None

    @property
    def standby_session(self) -> Optional[SellerSession]:
        return self._sessions[1] if len(self._sessions) > 1 else None

    def _target_session_count(self) -> int:
        return min(2, len(self._accounts))

    async def start(self):
        async with self._lock:
            self._sanitize_accounts_locked()
            self._persist_accounts_locked()
        self._schedule_recovery()

    async def close(self):
        self._stopped = True
        if self._recovery_task and not self._recovery_task.done():
            self._recovery_task.cancel()
            try:
                await self._recovery_task
            except asyncio.CancelledError:
                pass

        async with self._lock:
            now = _now_local()
            for acct in self._accounts:
                if acct.get("login_in_progress"):
                    started_at = _parse_time(acct.get("last_login_started_at")) or now
                    acct["status"] = "cooldown"
                    acct["last_login_error"] = acct.get("last_login_error") or "login_interrupted"
                    acct["cooldown_until"] = _dt_to_iso(
                        _ensure_retry_cooldown(
                            _parse_time(acct.get("cooldown_until")),
                            started_at=started_at,
                        )
                    )
                    acct["login_in_progress"] = False
                    acct["last_login_started_at"] = None
            self._persist_accounts_locked()
            sessions = list(self._sessions)
            self._sessions = []
            self._session_inflight.clear()
            self._session_soft_failures.clear()
            self._rr_cursor = 0
        for session in sessions:
            if session:
                await session.close()

    def _schedule_recovery(self):
        if self._stopped:
            return
        if not self._recovery_task or self._recovery_task.done():
            self._recovery_task = asyncio.create_task(self._recovery_loop())
        self._recovery_wakeup.set()

    async def _recovery_loop(self):
        while not self._stopped:
            try:
                await self.ensure_pool()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._last_recovery_error = str(e)
                log.error("seller recovery loop error: %s", e)

            if self._stopped:
                return

            self._recovery_wakeup.clear()
            try:
                await asyncio.wait_for(
                    self._recovery_wakeup.wait(),
                    timeout=SELLER_ACCOUNT_RECOVERY_INTERVAL_SECONDS,
                )
            except asyncio.TimeoutError:
                pass

    async def ensure_pool(self):
        target_count = self._target_session_count()
        while not self._stopped:
            async with self._lock:
                if self._stopped:
                    return
                self._sanitize_accounts_locked()
                await self._drop_dead_sessions_locked()
                self._persist_accounts_locked()
                if self._session_count_locked() >= target_count:
                    return

            async with self._lock:
                if self._session_count_locked() >= target_count:
                    return
                restore_candidates = self._restore_candidates_locked()
            progressed = False
            for acct in restore_candidates:
                if await self._has_session_email(acct["email"]):
                    continue
                session = await self._attempt_restore(acct)
                if session:
                    await self._add_session(session)
                    progressed = True
                    break
            if progressed:
                continue

            async with self._lock:
                if self._session_count_locked() >= target_count:
                    return
                login_candidates = self._login_candidates_locked()
            for acct in login_candidates:
                if await self._has_session_email(acct["email"]):
                    continue
                session = await self._attempt_login(acct)
                if session:
                    await self._add_session(session)
                    progressed = True
                    break
            if not progressed:
                return

    async def call_with_failover(self, method_name: str, *args, **kwargs):
        last_error: Optional[SellerSessionUnavailable] = None
        tried_emails: set[str] = set()

        while True:
            async with self._lock:
                await self._drop_dead_sessions_locked()
                session = self._checkout_session_locked(tried_emails)
                if not session:
                    self._schedule_recovery()
                    raise last_error or SellerSessionUnavailable("no healthy seller session")

            try:
                async with session._request_lock:
                    result = await getattr(session, method_name)(*args, **kwargs)
                    soft_failed = self._result_is_soft_failure(method_name, result)
            except SellerSessionUnavailable as e:
                last_error = e
                tried_emails.add(session.email)
                async with self._lock:
                    self._release_session_locked(session)
                    removed = self._remove_session_locked(
                        session,
                        acct_status="failed",
                        reason=str(e),
                    )
                    if removed:
                        self._schedule_recovery()
                if removed:
                    log.warning("seller pool removed failed session: %s (%s)", session.email, e)
                    await session.close()
                continue

            async with self._lock:
                remove = False
                if soft_failed:
                    failure_count = self._mark_session_health_locked(session.email, ok=False)
                    remove = failure_count >= SELLER_SESSION_SOFT_FAILURE_THRESHOLD
                    if remove:
                        removed = self._remove_session_locked(
                            session,
                            acct_status="stale",
                            reason=f"soft_request_failures:{method_name}",
                        )
                        if removed:
                            self._schedule_recovery()
                    else:
                        removed = False
                else:
                    self._mark_session_health_locked(session.email, ok=True)
                    removed = False
                if not remove:
                    self._release_session_locked(session)
            if soft_failed and removed:
                tried_emails.add(session.email)
                log.warning(
                    "seller pool removed soft-failing session: %s (%s)",
                    session.email,
                    method_name,
                )
                await session.close()
                continue
            return result

    async def health_snapshot(self) -> Dict[str, Any]:
        async with self._lock:
            self._sanitize_accounts_locked()
            await self._drop_dead_sessions_locked()
            session_emails = [session.email for session in self._sessions]
            return {
                "ready": bool(self._sessions),
                "mode": "multi_master",
                "session_count": len(self._sessions),
                "session_emails": session_emails,
                "active_email": self.active_session.email if self.active_session else None,
                "standby_email": self.standby_session.email if self.standby_session else None,
                "active_storage": str(self.active_session.storage_state_file) if self.active_session else None,
                "standby_storage": str(self.standby_session.storage_state_file) if self.standby_session else None,
                "recovery_running": bool(self._recovery_task and not self._recovery_task.done()),
                "last_failure": self._last_failure,
                "last_recovery_error": self._last_recovery_error,
                "accounts": [
                    {
                        "email": acct["email"],
                        "status": acct.get("status"),
                        "state_file": acct["state_file"],
                        "profile_dir": acct["profile_dir"],
                        "last_state_ok_at": acct.get("last_state_ok_at"),
                        "last_login_ok_at": acct.get("last_login_ok_at"),
                        "last_login_error": acct.get("last_login_error"),
                        "cooldown_until": acct.get("cooldown_until"),
                        "login_in_progress": acct.get("login_in_progress", False),
                    }
                    for acct in self._accounts
                ],
            }

    async def _add_session(self, session: SellerSession):
        to_close: Optional[SellerSession] = None
        async with self._lock:
            if self._stopped:
                to_close = session
            else:
                acct = self._account_locked(session.email)
                if acct:
                    acct["status"] = "ready"
                    acct["last_state_ok_at"] = _iso_now()
                    acct["last_login_ok_at"] = acct.get("last_login_ok_at") or _iso_now()
                    acct["last_login_error"] = ""
                    acct["cooldown_until"] = None
                    acct["login_in_progress"] = False
                    acct["last_login_started_at"] = None

                replace_index = next(
                    (idx for idx, current in enumerate(self._sessions) if current.email == session.email),
                    None,
                )
                if replace_index is not None:
                    to_close = self._sessions[replace_index]
                    self._sessions[replace_index] = session
                    self._session_inflight.setdefault(session.email, 0)
                    self._session_soft_failures.pop(session.email, None)
                elif len(self._sessions) < self._target_session_count():
                    self._sessions.append(session)
                    self._session_inflight.setdefault(session.email, 0)
                    self._session_soft_failures.pop(session.email, None)
                    log.info("seller session ready = %s", session.email)
                else:
                    to_close = session
                if self._sessions:
                    self._rr_cursor %= len(self._sessions)
                else:
                    self._rr_cursor = 0
                self._persist_accounts_locked()

        if to_close:
            await to_close.close()
        if self._stopped:
            return

    async def _has_session_email(self, email: str) -> bool:
        async with self._lock:
            return any(session.email == email for session in self._sessions)

    async def _is_pool_full(self, target_count: int) -> bool:
        async with self._lock:
            return self._session_count_locked() >= target_count

    def _session_count_locked(self) -> int:
        return len(self._sessions)

    def _ordered_sessions_locked(self) -> list[SellerSession]:
        if not self._sessions:
            return []
        start = self._rr_cursor % len(self._sessions)
        return self._sessions[start:] + self._sessions[:start]

    def _checkout_session_locked(self, exclude_emails: set[str]) -> Optional[SellerSession]:
        ordered = self._ordered_sessions_locked()
        candidates = [session for session in ordered if session.email not in exclude_emails]
        if not candidates:
            return None

        best_index = 0
        best_key = None
        for idx, session in enumerate(candidates):
            key = (self._session_inflight.get(session.email, 0), idx)
            if best_key is None or key < best_key:
                best_key = key
                best_index = idx
        chosen = candidates[best_index]
        self._session_inflight[chosen.email] = self._session_inflight.get(chosen.email, 0) + 1
        if self._sessions:
            chosen_index = self._sessions.index(chosen)
            self._rr_cursor = (chosen_index + 1) % len(self._sessions)
        return chosen

    def _release_session_locked(self, session: SellerSession):
        current = self._session_inflight.get(session.email, 0)
        if current <= 1:
            self._session_inflight.pop(session.email, None)
        else:
            self._session_inflight[session.email] = current - 1

    def _mark_session_health_locked(self, email: str, ok: bool) -> int:
        if ok:
            self._session_soft_failures.pop(email, None)
            return 0
        failures = self._session_soft_failures.get(email, 0) + 1
        self._session_soft_failures[email] = failures
        return failures

    @staticmethod
    def _result_is_soft_failure(method_name: str, result: Any) -> bool:
        if method_name == "fetch_variant_model_result":
            return isinstance(result, dict) and result.get("status") == VARIANT_MODEL_STATUS_REQUEST_FAILED
        if method_name == "fetch_data_v3":
            return result is None
        return False

    def _remove_session_locked(self, session: SellerSession, acct_status: str, reason: str) -> bool:
        remove_index = next(
            (idx for idx, current in enumerate(self._sessions) if current is session),
            None,
        )
        if remove_index is None:
            return False
        self._sessions.pop(remove_index)
        self._session_inflight.pop(session.email, None)
        self._session_soft_failures.pop(session.email, None)
        acct = self._account_locked(session.email)
        if acct:
            acct["status"] = acct_status
            acct["last_login_error"] = reason
        self._last_failure = {
            "email": session.email,
            "reason": reason,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if self._sessions:
            self._rr_cursor %= len(self._sessions)
        else:
            self._rr_cursor = 0
        self._persist_accounts_locked()
        return True

    async def _drop_dead_sessions_locked(self):
        dead_sessions = [session for session in list(self._sessions) if not session.is_shallow_ready()]
        for session in dead_sessions:
            removed = self._remove_session_locked(
                session,
                acct_status="stale",
                reason="session_dead",
            )
            if not removed:
                continue
            self._last_failure = {
                "email": session.email,
                "reason": "shallow_check_failed",
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            log.warning("seller session dead: %s", session.email)
            await session.close()


    def _account_locked(self, email: str) -> Optional[dict]:
        for acct in self._accounts:
            if acct["email"] == email:
                return acct
        return None

    def _sanitize_accounts_locked(self):
        changed = False
        now = _now_local()
        for acct in self._accounts:
            acct.setdefault("status", "unknown")
            acct.setdefault("state_file", _account_storage_state(acct["email"]))
            acct.setdefault("profile_dir", _account_profile_dir(acct["email"]))
            acct.setdefault("last_state_ok_at", None)
            acct.setdefault("last_login_ok_at", None)
            acct.setdefault("last_login_error", "")
            acct.setdefault("cooldown_until", None)
            acct.setdefault("login_in_progress", False)
            acct.setdefault("last_login_started_at", None)
            started_at = _parse_time(acct.get("last_login_started_at"))
            if acct.get("login_in_progress"):
                stale_from_previous_run = not started_at or started_at < self._boot_time
                stale_by_timeout = started_at and (now - started_at).total_seconds() > SELLER_LOGIN_PROGRESS_STALE_SECONDS
                if stale_from_previous_run or stale_by_timeout:
                    acct["status"] = "cooldown"
                    acct["last_login_error"] = acct.get("last_login_error") or "login_interrupted"
                    acct["cooldown_until"] = _dt_to_iso(
                        _ensure_retry_cooldown(
                            _parse_time(acct.get("cooldown_until")),
                            started_at=started_at or now,
                        )
                    )
                    acct["login_in_progress"] = False
                    acct["last_login_started_at"] = None
                    changed = True
        if changed:
            self._persist_accounts_locked()

    def _persist_accounts_locked(self):
        payload = {"seller_accounts": [_serialize_account(acct) for acct in self._accounts]}
        self._account_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _restore_candidates_locked(self) -> list[dict]:
        candidates = []
        for acct in self._accounts:
            if Path(acct["state_file"]).exists():
                candidates.append(acct)
        return sorted(candidates, key=_account_state_score, reverse=True)

    def _login_candidates_locked(self) -> list[dict]:
        candidates = []
        for acct in self._accounts:
            if acct.get("login_in_progress"):
                continue
            if _cooldown_active(acct):
                continue
            candidates.append(acct)
        return sorted(candidates, key=_account_state_score, reverse=True)

    async def _open_session(self, acct: dict, allow_login: bool) -> tuple[Optional[SellerSession], Optional[SellerSession]]:
        session = SellerSession(
            acct["email"],
            acct["app_password"],
            acct["client_id"],
            storage_state_file=acct["state_file"],
            cdp_port=acct["cdp_port"],
            profile_dir=acct["profile_dir"],
        )
        ok = await session.start(allow_login=allow_login)
        if ok:
            return session, None
        return None, session

    async def _attempt_restore(self, acct: dict) -> Optional[SellerSession]:
        if self._stopped:
            return None
        log.info("优先尝试恢复 seller state: %s (%s)", acct["email"], acct["state_file"])
        session, failed = await self._open_session(acct, allow_login=False)
        if session:
            async with self._lock:
                acct["status"] = "ready"
                acct["last_state_ok_at"] = _iso_now()
                acct["last_login_error"] = ""
                self._persist_accounts_locked()
            return session
        async with self._lock:
            acct["status"] = "stale"
            if not acct.get("last_login_error"):
                acct["last_login_error"] = "state_restore_failed"
            self._persist_accounts_locked()
        if failed:
            await failed.close()
            failed.purge_session_artifacts("state_restore_failed")
        return None

    async def _attempt_login(self, acct: dict) -> Optional[SellerSession]:
        if self._stopped:
            return None
        async with self._lock:
            acct["login_in_progress"] = True
            acct["last_login_started_at"] = _iso_now()
            acct["status"] = "logging_in"
            self._persist_accounts_locked()

        log.info("后台静默登录 seller 账号: %s", acct["email"])
        session, failed = await self._open_session(acct, allow_login=True)
        if session:
            async with self._lock:
                acct["status"] = "ready"
                acct["last_login_ok_at"] = _iso_now()
                acct["last_state_ok_at"] = _iso_now()
                acct["last_login_error"] = ""
                acct["cooldown_until"] = None
                acct["login_in_progress"] = False
                acct["last_login_started_at"] = None
                self._persist_accounts_locked()
            return session

        cooldown_dt = _ensure_retry_cooldown(
            failed.cooldown_until if failed else None,
            started_at=_parse_time(acct.get("last_login_started_at")),
        )
        failure_reason = failed.login_failure_reason if failed and failed.login_failure_reason else "login_failed"
        async with self._lock:
            acct["status"] = "cooldown"
            acct["last_login_error"] = failure_reason
            acct["cooldown_until"] = _dt_to_iso(cooldown_dt)
            acct["login_in_progress"] = False
            acct["last_login_started_at"] = None
            self._persist_accounts_locked()
        if failed:
            await failed.hold_before_close(failure_reason)
            await failed.close()
        return None
