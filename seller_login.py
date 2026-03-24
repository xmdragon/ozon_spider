"""
Ozon Seller 登录模块。

启动独立系统 Chrome（独立 user-data-dir，CDP 端口 9224），
与 spider Chrome（端口 9223）完全 profile 隔离。
登录后 session 常驻，供后续请求 search-variant-mode 使用。
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional, Dict, Any

from chrome_launcher import start_chrome, kill
from config import CHROME_BIN, XVFB_DISPLAY

log = logging.getLogger(__name__)

SELLER_CDP_PORT = 9224
SELLER_DASHBOARD = "https://seller.ozon.ru/app/dashboard"
SELLER_LOGIN_URL = "https://seller.ozon.ru/"


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
                 cdp_port: int = SELLER_CDP_PORT):
        self.email = email
        self.app_password = app_password
        self.client_id = client_id
        self.storage_state_file = Path(storage_state_file)
        self.cdp_port = cdp_port
        self._chrome_proc = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    @property
    def page(self):
        return self._page

    @staticmethod
    def _is_authenticated_seller_url(url: str) -> bool:
        return (
            "seller.ozon.ru/app" in url
            and not any(k in url for k in ("signin", "ozonid", "sso", "registration"))
        )

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
            if t in ('text', 'email', ''):
                return inp
        return None

    async def _click_next_button(self) -> bool:
        try:
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
        await asyncio.sleep(5)
        return True

    async def _handle_existing_authenticated_flow(self) -> bool:
        """处理 2FA/回落 signin 后只需点“下一步”的已认证流程。"""
        if not self._page:
            return False

        saw_2fa = False
        for _ in range(12):
            url = self._page.url
            if self._is_authenticated_seller_url(url):
                return True

            if "auth/2fa" in url:
                if not saw_2fa:
                    saw_2fa = True
                    log.info("检测到 2FA 过渡页，等待其自动跳转")
                await asyncio.sleep(2)
                continue

            if await self._click_next_button():
                if self._is_authenticated_seller_url(self._page.url):
                    return True
                await asyncio.sleep(2)
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

            await asyncio.sleep(2)

        return self._is_authenticated_seller_url(self._page.url)

    async def start(self, allow_login: bool = True) -> bool:
        """
        启动 seller Chrome 并建立 session。
        先尝试恢复已有 session（storage_state），失败则重新登录。
        """
        from playwright.async_api import async_playwright

        # 每个账号使用独立的 Chrome profile（按 email 区分）
        os.environ["DISPLAY"] = XVFB_DISPLAY
        profile_name = "seller_profile_" + self.email.split("@")[0]
        seller_profile = Path(profile_name)
        seller_profile.mkdir(exist_ok=True)
        self._chrome_proc = start_chrome(CHROME_BIN, self.cdp_port, XVFB_DISPLAY,
                                         user_data_dir=str(seller_profile.absolute()))

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self.cdp_port}"
        )
        self._context = self._browser.contexts[0] if self._browser.contexts else \
            await self._browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow")
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
            await self._page.goto(SELLER_DASHBOARD, timeout=30000, wait_until="domcontentloaded")
            await asyncio.sleep(5)
            url = self._page.url
            log.info("恢复 session 后 URL: %s", url)
            # 只有确实在 signin/ozonid/sso 才算失败
            if any(k in url for k in ["signin", "ozonid", "sso.ozon"]):
                return False
            log.info("session 验证通过")
            return True
        except Exception as e:
            log.warning("恢复 session 失败: %s", e)
            return False

    async def _do_login(self) -> bool:
        """执行完整邮箱登录流程。"""
        from email_service import EmailService

        try:
            # 直接访问 seller 登录页（跳过首页 → ozonid 跳转，避免 antibot）
            signin_url = "https://seller.ozon.ru/app/registration/signin?locale=zh-Hans"
            log.info("访问登录页: %s", signin_url)
            await self._page.goto(signin_url, timeout=60000, wait_until="domcontentloaded")
            try:
                await self._page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                await asyncio.sleep(3)

            log.info("登录页 URL: %s", self._page.url)
            await asyncio.sleep(3)

            # 步骤1: 点击「登录」按钮
            login_btn = await self._page.query_selector('button[type="submit"]:has-text("登录"), button:has-text("Войти")')
            if login_btn:
                await login_btn.evaluate('el => el.click()')
                log.info("点击登录按钮")
                await asyncio.sleep(8)  # 等待 ozonid 页面加载 + antibot JS 通过
            log.info("点击登录后 URL: %s", self._page.url)

            if await self._handle_existing_authenticated_flow():
                log.info("检测到已认证流程，无需重新收验证码")
            else:
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
                    await asyncio.sleep(2)

                await asyncio.sleep(3)
                log.info("当前 URL: %s", self._page.url)

                # 填邮箱
                email_input = await self._find_email_input()
                if not email_input:
                    if await self._handle_existing_authenticated_flow():
                        log.info("回落 signin 后通过下一步进入已认证流程")
                    else:
                        log.error("未找到邮箱输入框，URL: %s", self._page.url)
                        return False
                else:
                    await email_input.fill(self.email)
                    log.info("邮箱已输入: %s", self.email)
                    await asyncio.sleep(1)

                    # 记录发送前最新邮件ID（必须在点击发送前记录）
                    email_svc = EmailService(self.email, self.app_password)
                    code = None
                    folder_last_ids = {}
                    with email_svc:
                        email_svc.connect_imap()
                        try:
                            for folder in email_svc.get_check_folders(check_spam=True):
                                ids = email_svc.list_email_ids(folder)
                                folder_last_ids[folder] = ids[-1] if ids else 0
                            log.info("发送前各文件夹最新邮件ID: %s", folder_last_ids)
                        except Exception as e:
                            log.warning("获取邮件ID失败: %s", e)
                            folder_last_ids = {"INBOX": 0}

                    # 提交
                    send_btn = await self._page.query_selector(
                        'button:has-text("Отправить"), button:has-text("Продолжить"), button[type="submit"]'
                    )
                    if send_btn:
                        await send_btn.click()
                    else:
                        await self._page.keyboard.press("Enter")
                    log.info("发送验证码请求")
                    await asyncio.sleep(5)  # 等待 ozon 发送邮件

                    # 等待验证码（只取ID > last_id 的新邮件）
                    log.info("等待验证码邮件...")
                    with email_svc:
                        email_svc.connect_imap()
                        deadline = time.time() + 60
                        while time.time() < deadline:
                            for folder in list(folder_last_ids):
                                try:
                                    all_ids = email_svc.list_email_ids(folder)
                                    new_ids = [x for x in all_ids if x > folder_last_ids.get(folder, 0)]
                                    for eid in reversed(new_ids):
                                        mail = email_svc.fetch_email_by_id(folder, eid)
                                        if not mail or not email_svc.is_ozon_verification_email(mail["from"], mail["subject"]):
                                            continue
                                        if not email_svc.is_email_within_seconds(mail["date"], 60):
                                            continue
                                        c = email_svc._extract_ozon_code(mail["body"], mail["subject"])
                                        if c:
                                            code = c
                                            log.info("收到新验证码: %s (folder=%s, ID=%d)", c, folder, eid)
                                            break
                                    if all_ids:
                                        folder_last_ids[folder] = all_ids[-1]
                                    if code:
                                        break
                                except Exception as e:
                                    log.warning("检查文件夹 %s 失败: %s", folder, e)
                            if code:
                                break
                            time.sleep(3)

                    if not code:
                        log.error("未收到验证码")
                        return False
                    log.info("收到验证码")

                    # 填入验证码（重新查询避免旧句柄过期）
                    await asyncio.sleep(2)
                    await self._page.evaluate(
                        '([sel, val]) => { const el = document.querySelector(sel); '
                        'if(el){ el.value=val; el.dispatchEvent(new Event("input",{bubbles:true})); el.dispatchEvent(new Event("change",{bubbles:true})); } }',
                        ['input[type="text"]', code]
                    )
                    log.info("验证码已填入")

                    # 等待页面导航完成
                    try:
                        await self._page.wait_for_load_state("domcontentloaded", timeout=10000)
                    except Exception:
                        pass
                    await asyncio.sleep(2)
                    log.info("验证码后 URL: %s", self._page.url)

                    # 检测并点击「下一步」
                    for _ in range(10):
                        if await self._click_next_button():
                            break
                        if self._is_authenticated_seller_url(self._page.url):
                            break
                        await asyncio.sleep(2)

            log.info("下一步后 URL: %s", self._page.url)

            # 如果还在 signin，再点一次登录按钮
            if "signin" in self._page.url or "registration" in self._page.url:
                btn = await self._page.query_selector('button[type="submit"]:has-text("登录"), button:has-text("Войти")')
                if btn:
                    await btn.evaluate('el => el.click()')
                    log.info("再次点击登录按钮")
                    await asyncio.sleep(8)

            log.info("最终 URL: %s", self._page.url)

            # 验证：不在认证页即为成功
            final_url = self._page.url
            if any(k in final_url for k in ("signin", "ozonid", "sso", "registration")):
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
        """用轻量 seller API 检查当前 session 是否可用。"""
        try:
            status, _ = await self._page_fetch(
                "https://seller.ozon.ru/api/v1/search-variant-model",
                {"limit": "1", "name": "1"},
            )
            return status == 200
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

    async def fetch_variant_model(self, sku: str) -> Optional[Dict[str, Any]]:
        """
        请求 search-variant-model 获取单个 SKU 的尺寸/重量。
        返回 {"weight": g, "depth": mm, "width": mm, "height": mm} 或 None。
        """
        try:
            status, data = await self._page_fetch(
                "https://seller.ozon.ru/api/v1/search-variant-model",
                {"limit": "10", "name": str(sku)},
            )
            if status in (401, 403):
                raise SellerSessionUnavailable(f"variant-model unauthorized for {self.email}: status={status}")
            if status != 200 or not data:
                log.warning("variant-model SKU %s status=%d", sku, status)
                return None
            items = data.get("items", [])
            if not items:
                return None
            attrs = items[0].get("attributes", [])
            dimensions = {}
            for attr in attrs:
                key = str(attr.get("key", ""))
                if key in self._DIMENSION_ATTR_KEYS:
                    try:
                        dimensions[self._DIMENSION_ATTR_KEYS[key]] = float(attr["value"])
                    except (ValueError, KeyError):
                        pass
            log.info("SKU %s dimensions: %s", sku, dimensions)
            return dimensions or None
        except SellerSessionUnavailable:
            raise
        except Exception as e:
            msg = str(e)
            if "closed" in msg.lower() or "target page" in msg.lower():
                raise SellerSessionUnavailable(f"variant-model page closed for {self.email}: {e}") from e
            log.warning("fetch_variant_model error: %s", e)
            return None

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


async def get_seller_session(
    email: str, app_password: str, client_id: str,
    storage_state_file: str = "seller_state.json",
    allow_login: bool = True,
    cdp_port: int = SELLER_CDP_PORT,
) -> Optional[SellerSession]:
    """
    创建并启动 SellerSession。返回就绪的 session，失败返回 None。
    """
    session = SellerSession(email, app_password, client_id, storage_state_file, cdp_port=cdp_port)
    ok = await session.start(allow_login=allow_login)
    if ok:
        return session
    await session.close()
    return None


def _account_storage_state(email: str) -> str:
    return f"seller_state_{email.split('@')[0]}.json"


def _account_cdp_port(index: int) -> int:
    return SELLER_CDP_PORT + index


async def get_seller_session_with_fallback(accounts: list) -> Optional[SellerSession]:
    """
    尝试多个账号，返回第一个成功的 SellerSession。
    accounts: [{email, app_password, client_id}, ...]
    每个账号使用独立的 storage_state 文件（seller_state_{email}.json）。
    """
    # 第一阶段：优先尝试恢复现有 cookies，不触发重新登录
    for idx, acct in enumerate(accounts):
        email = acct["email"]
        storage = _account_storage_state(email)
        if not Path(storage).exists():
            continue
        log.info("优先尝试恢复账号 cookies: %s (%s)", email, storage)
        session = await get_seller_session(
            email,
            acct["app_password"],
            acct["client_id"],
            storage,
            allow_login=False,
            cdp_port=_account_cdp_port(idx),
        )
        if session:
            log.info("✓ 使用已有 cookies 恢复账号成功: %s", email)
            return session
        log.warning("账号 %s 的已有 cookies 不可用，继续尝试其他 cookies", email)

    # 第二阶段：所有已有 cookies 都不可用时，才按配置顺序登录
    for idx, acct in enumerate(accounts):
        email = acct["email"]
        storage = _account_storage_state(email)
        log.info("尝试账号: %s", email)
        session = await get_seller_session(
            email,
            acct["app_password"],
            acct["client_id"],
            storage,
            cdp_port=_account_cdp_port(idx),
        )
        if session:
            log.info("✓ 账号 %s 登录成功", email)
            return session
        log.warning("账号 %s 登录失败，尝试下一个", email)
    log.error("所有账号登录均失败")
    return None


class SellerSessionManager:
    """Manage active/standby seller sessions with background recovery."""

    def __init__(self, accounts: list):
        self._accounts = [
            {
                **acct,
                "storage_state_file": _account_storage_state(acct["email"]),
                "cdp_port": _account_cdp_port(idx),
            }
            for idx, acct in enumerate(accounts)
        ]
        self.active_session: Optional[SellerSession] = None
        self.standby_session: Optional[SellerSession] = None
        self._lock = asyncio.Lock()
        self._recovery_task: Optional[asyncio.Task] = None
        self._stopped = False
        self._last_failure: Optional[Dict[str, Any]] = None
        self._last_recovery_error: Optional[str] = None

    async def start(self):
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
            sessions = [self.active_session, self.standby_session]
            self.active_session = None
            self.standby_session = None
        for session in sessions:
            if session:
                await session.close()

    def _schedule_recovery(self):
        if self._stopped:
            return
        if self._recovery_task and not self._recovery_task.done():
            return
        self._recovery_task = asyncio.create_task(self._recovery_loop())

    async def _recovery_loop(self):
        try:
            await self.ensure_pool()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._last_recovery_error = str(e)
            log.error("seller recovery loop error: %s", e)

    async def ensure_pool(self):
        target_count = min(2, len(self._accounts))
        async with self._lock:
            if self._stopped:
                return
            await self._drop_dead_sessions_locked()
            if self._session_count_locked() >= target_count:
                return

        # phase 1: restore existing cookies only
        for acct in self._accounts:
            if self._stopped:
                return
            if not Path(acct["storage_state_file"]).exists():
                continue
            if await self._has_session_email(acct["email"]):
                continue
            log.info("后台恢复 seller cookies: %s", acct["email"])
            session = await get_seller_session(
                acct["email"],
                acct["app_password"],
                acct["client_id"],
                acct["storage_state_file"],
                allow_login=False,
                cdp_port=acct["cdp_port"],
            )
            if session:
                await self._add_session(session)
                if await self._is_pool_full(target_count):
                    return

        # phase 2: login only in background
        for acct in self._accounts:
            if self._stopped:
                return
            if await self._has_session_email(acct["email"]):
                continue
            log.info("后台登录 seller 账号: %s", acct["email"])
            session = await get_seller_session(
                acct["email"],
                acct["app_password"],
                acct["client_id"],
                acct["storage_state_file"],
                allow_login=True,
                cdp_port=acct["cdp_port"],
            )
            if session:
                await self._add_session(session)
                if await self._is_pool_full(target_count):
                    return

    async def call_with_failover(self, method_name: str, *args, **kwargs):
        async with self._lock:
            await self._drop_dead_sessions_locked()
            session = self.active_session
            standby = self.standby_session

            if not session:
                self._schedule_recovery()
                raise SellerSessionUnavailable("no active seller session")

            try:
                return await getattr(session, method_name)(*args, **kwargs)
            except SellerSessionUnavailable as e:
                await self._retire_active_locked(str(e))
                if not standby:
                    self._schedule_recovery()
                    raise
                self.active_session = standby
                self.standby_session = None
                log.warning("seller failover: promote standby %s", standby.email)
                try:
                    result = await getattr(self.active_session, method_name)(*args, **kwargs)
                except SellerSessionUnavailable as standby_error:
                    await self._retire_active_locked(str(standby_error))
                    self._schedule_recovery()
                    raise
                self._schedule_recovery()
                return result

    async def health_snapshot(self) -> Dict[str, Any]:
        async with self._lock:
            await self._drop_dead_sessions_locked()
            return {
                "ready": self.active_session is not None,
                "active_email": self.active_session.email if self.active_session else None,
                "standby_email": self.standby_session.email if self.standby_session else None,
                "active_storage": str(self.active_session.storage_state_file) if self.active_session else None,
                "standby_storage": str(self.standby_session.storage_state_file) if self.standby_session else None,
                "recovery_running": bool(self._recovery_task and not self._recovery_task.done()),
                "last_failure": self._last_failure,
                "last_recovery_error": self._last_recovery_error,
            }

    async def _add_session(self, session: SellerSession):
        async with self._lock:
            if self._stopped:
                await session.close()
                return
            if self.active_session and self.active_session.email == session.email:
                await self.active_session.close()
                self.active_session = session
                return
            if self.standby_session and self.standby_session.email == session.email:
                await self.standby_session.close()
                self.standby_session = session
                return
            if self.active_session is None:
                self.active_session = session
                log.info("seller active session = %s", session.email)
            elif self.standby_session is None:
                self.standby_session = session
                log.info("seller standby session = %s", session.email)
            else:
                await session.close()

    async def _has_session_email(self, email: str) -> bool:
        async with self._lock:
            return any(
                session and session.email == email
                for session in (self.active_session, self.standby_session)
            )

    async def _is_pool_full(self, target_count: int) -> bool:
        async with self._lock:
            return self._session_count_locked() >= target_count

    def _session_count_locked(self) -> int:
        return int(self.active_session is not None) + int(self.standby_session is not None)

    async def _drop_dead_sessions_locked(self):
        for role in ("active_session", "standby_session"):
            session = getattr(self, role)
            if not session:
                continue
            if session.is_shallow_ready():
                continue
            setattr(self, role, None)
            self._last_failure = {
                "email": session.email,
                "reason": "shallow_check_failed",
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            log.warning("seller session dead: %s", session.email)
            await session.close()
        if self.active_session is None and self.standby_session is not None:
            self.active_session = self.standby_session
            self.standby_session = None
            log.info("seller failover: standby promoted to active = %s", self.active_session.email)

    async def _retire_active_locked(self, reason: str):
        failed = self.active_session
        if not failed:
            return
        self.active_session = None
        self._last_failure = {
            "email": failed.email,
            "reason": reason,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        log.warning("seller active session retired: %s (%s)", failed.email, reason)
        await failed.close()
