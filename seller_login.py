"""
Ozon Seller 登录模块。

独立 Chrome 实例（与 spider 隔离），登录后 session 常驻，
后续可在同一 page 上请求 search-variant-mode 获取尺寸/重量数据。
"""
import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional, Dict, Any

log = logging.getLogger(__name__)

SELLER_DASHBOARD = "https://seller.ozon.ru/app/dashboard"
SELLER_LOGIN_URL = "https://seller.ozon.ru/"


class SellerSession:
    """
    管理 Ozon Seller 的独立浏览器 session。
    登录后 browser/context/page 保持常驻，供后续 API 请求使用。
    """

    def __init__(self, email: str, app_password: str, client_id: str,
                 storage_state_file: str = "seller_state.json"):
        self.email = email
        self.app_password = app_password
        self.client_id = client_id
        self.storage_state_file = Path(storage_state_file)
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._user_data_dir = None

    @property
    def page(self):
        return self._page

    async def start(self) -> bool:
        """
        启动 seller 浏览器。先尝试恢复已有 session，失败则重新登录。
        Returns True 表示 session 就绪。
        """
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._user_data_dir = tempfile.mkdtemp(prefix="ozon_seller_")

        # 尝试从 storage_state 恢复
        if self.storage_state_file.exists():
            log.info("尝试恢复已有 seller session...")
            ok = await self._launch_with_state()
            if ok:
                log.info("✓ Seller session 恢复成功")
                return True
            log.warning("Session 已失效，重新登录")
            await self._close_browser()

        # 重新登录
        log.info("开始 seller 登录流程...")
        ok = await self._do_login()
        return ok

    async def _launch_with_state(self) -> bool:
        """使用已有 storage_state 启动浏览器并验证 session。"""
        try:
            self._browser = await self._playwright.chromium.launch(
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--window-size=1280,800",
                ],
            )
            self._context = await self._browser.new_context(
                storage_state=str(self.storage_state_file),
                viewport={"width": 1280, "height": 800},
                locale="ru-RU",
                timezone_id="Europe/Moscow",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            self._page = await self._context.new_page()
            await self._page.goto(SELLER_DASHBOARD, timeout=30000, wait_until="domcontentloaded")
            await asyncio.sleep(3)
            url = self._page.url
            if "signin" in url or "ozonid" in url or "registration" in url:
                return False
            log.info("session 验证通过，当前 URL: %s", url)
            return True
        except Exception as e:
            log.warning("恢复 session 失败: %s", e)
            return False

    async def _do_login(self) -> bool:
        """执行完整邮箱登录流程。"""
        from email_service import EmailService

        try:
            self._browser = await self._playwright.chromium.launch(
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--window-size=1280,800",
                ],
            )
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="ru-RU",
                timezone_id="Europe/Moscow",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            self._page = await self._context.new_page()

            log.info("访问登录页: %s", SELLER_LOGIN_URL)
            await self._page.goto(SELLER_LOGIN_URL, timeout=60000, wait_until="domcontentloaded")
            try:
                await self._page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                await asyncio.sleep(3)

            # 点击「Войти」按钮（如果有）
            for sel in ['button:has-text("Войти")', 'text="Войти" >> nth=0']:
                btn = await self._page.query_selector(sel)
                if btn:
                    text = (await btn.text_content() or "").strip()
                    if text == "Войти":
                        await btn.click()
                        log.info("点击了 Войти 按钮")
                        await asyncio.sleep(3)
                        break

            # 点击「Войти по почте」
            email_btn_clicked = False
            for _ in range(10):
                for sel in ['text="Войти по почте"', 'a:has-text("Войти по почте")',
                            'button:has-text("по почте")', '[data-testid*="email"]']:
                    btn = await self._page.query_selector(sel)
                    if btn:
                        await btn.click()
                        log.info("点击了邮箱登录: %s", sel)
                        email_btn_clicked = True
                        break
                if email_btn_clicked:
                    break
                await asyncio.sleep(2)

            await asyncio.sleep(3)

            # 填邮箱
            email_input = await self._page.query_selector(
                'input[type="email"], input[name="email"], '
                'input[placeholder*="почт"], input[placeholder*="email"]'
            )
            if email_input:
                await email_input.fill(self.email)
                log.info("邮箱已输入: %s", self.email)
            else:
                log.error("未找到邮箱输入框")
                return False

            await asyncio.sleep(1)

            # 提交邮箱
            send_btn = await self._page.query_selector(
                'button:has-text("Отправить"), button:has-text("Продолжить"), button[type="submit"]'
            )
            if send_btn:
                await send_btn.click()
            else:
                await self._page.keyboard.press("Enter")
            log.info("发送验证码请求")
            await asyncio.sleep(3)

            # 等待验证码邮件
            log.info("等待验证码邮件...")
            email_svc = EmailService(self.email, self.app_password)
            with email_svc:
                email_svc.connect_imap()
                code = email_svc.wait_for_ozon_code(timeout=55, interval=3, check_spam=True)

            if not code:
                log.error("未收到验证码")
                return False

            log.info("收到验证码: %s", code)

            # 填验证码
            code_inputs = await self._page.query_selector_all(
                'input[maxlength="1"], input[inputmode="numeric"][maxlength="1"]'
            )
            if len(code_inputs) >= 4:
                for i, digit in enumerate(code[:len(code_inputs)]):
                    await code_inputs[i].fill(digit)
                    await asyncio.sleep(0.1)
                log.info("验证码逐位填入")
            else:
                single = await self._page.query_selector(
                    'input[autocomplete="one-time-code"], input[name*="code"], '
                    'input[placeholder*="код"]'
                )
                if single:
                    await single.fill(code)
                    log.info("验证码填入（单框）")
                else:
                    log.error("未找到验证码输入框")
                    return False

            await asyncio.sleep(5)

            # 处理 SSO token 跳转
            url = self._page.url
            if "ozonid" in url and "token=" in url:
                log.info("检测到 SSO token，导航到 dashboard...")
                await self._page.goto(SELLER_DASHBOARD, timeout=30000, wait_until="domcontentloaded")
                await asyncio.sleep(3)
                url = self._page.url

            # 验证登录
            cookies = {c["name"]: c["value"] for c in await self._context.cookies()}
            if "__Secure-access-token" in cookies or "__Secure-refresh-token" in cookies:
                log.info("✓ 登录成功，保存 storage_state")
                await self._context.storage_state(path=str(self.storage_state_file))
                return True

            log.warning("登录后未检测到认证 Cookie，最终 URL: %s", url)
            return False

        except Exception as e:
            log.error("登录异常: %s", e, exc_info=True)
            return False

    async def fetch_variant_dimensions(self, sku: str) -> Optional[Dict[str, Any]]:
        """
        在已登录的 seller session 中请求 search-variant-mode 获取尺寸/重量。
        占位实现，待确认 API 端点后完善。
        """
        if not self._page:
            log.error("Seller session 未就绪")
            return None
        try:
            url = (
                f"https://seller.ozon.ru/api/site/search-variant-mode"
                f"?client_id={self.client_id}&sku={sku}"
            )
            result = await self._page.evaluate(f"""
                async () => {{
                    const r = await fetch('{url}', {{credentials: 'include'}});
                    return r.ok ? r.json() : null;
                }}
            """)
            log.info("SKU %s dimensions: %s", sku, result)
            return result
        except Exception as e:
            log.warning("fetch_variant_dimensions error: %s", e)
            return None

    async def _close_browser(self):
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        self._browser = None
        self._context = None
        self._page = None

    async def close(self):
        """关闭 seller 浏览器，释放资源。"""
        await self._close_browser()
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        if self._user_data_dir:
            import shutil
            try:
                shutil.rmtree(self._user_data_dir, ignore_errors=True)
            except Exception:
                pass


async def get_seller_session(
    email: str, app_password: str, client_id: str,
    storage_state_file: str = "seller_state.json",
) -> Optional[SellerSession]:
    """
    创建并启动 SellerSession。返回就绪的 session，失败返回 None。
    """
    session = SellerSession(email, app_password, client_id, storage_state_file)
    ok = await session.start()
    if ok:
        return session
    await session.close()
    return None
