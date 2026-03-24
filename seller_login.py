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


class SellerSession:
    """
    管理 Ozon Seller 的独立浏览器 session。
    使用系统 Chrome + 独立 user-data-dir，CDP 端口 9224。
    登录后 browser/context/page 保持常驻。
    """

    def __init__(self, email: str, app_password: str, client_id: str,
                 storage_state_file: str = "seller_state.json"):
        self.email = email
        self.app_password = app_password
        self.client_id = client_id
        self.storage_state_file = Path(storage_state_file)
        self._chrome_proc = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    @property
    def page(self):
        return self._page

    async def start(self) -> bool:
        """
        启动 seller Chrome 并建立 session。
        先尝试恢复已有 session（storage_state），失败则重新登录。
        """
        from playwright.async_api import async_playwright

        # 启动独立 Chrome 实例（固定 user-data-dir，持久化 cookies）
        os.environ["DISPLAY"] = XVFB_DISPLAY
        seller_profile = Path("seller_chrome_profile")
        seller_profile.mkdir(exist_ok=True)
        self._chrome_proc = start_chrome(CHROME_BIN, SELLER_CDP_PORT, XVFB_DISPLAY,
                                         user_data_dir=str(seller_profile.absolute()))

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{SELLER_CDP_PORT}"
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
            email_input = await self._page.query_selector(
                'input[type="email"], input[name="email"], '
                'input[placeholder*="почт"], input[placeholder*="email"]'
            )
            if not email_input:
                inputs = await self._page.query_selector_all('input')
                for inp in inputs:
                    t = await inp.get_attribute('type') or 'text'
                    if t in ('text', 'email', ''):
                        email_input = inp
                        break
            if not email_input:
                log.error("未找到邮箱输入框，URL: %s", self._page.url)
                return False

            await email_input.fill(self.email)
            log.info("邮箱已输入: %s", self.email)
            await asyncio.sleep(1)

            # 记录发送前最新邮件ID（必须在点击发送前记录）
            email_svc = EmailService(self.email, self.app_password)
            code = None
            last_id = 0
            with email_svc:
                email_svc.connect_imap()
                try:
                    imap = email_svc._imap_conn
                    imap.select("INBOX")
                    _, nums = imap.search(None, "ALL")
                    all_ids = nums[0].split() if nums[0] else []
                    last_id = int(all_ids[-1]) if all_ids else 0
                    log.info("发送前最新邮件ID: %d", last_id)
                except Exception as e:
                    log.warning("获取邮件ID失败: %s", e)

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
                import email as _email_mod
                deadline = time.time() + 60
                while time.time() < deadline:
                    try:
                        imap = email_svc._imap_conn
                        imap.select("INBOX")
                        _, nums = imap.search(None, "ALL")
                        all_ids = [int(x) for x in (nums[0].split() if nums[0] else [])]
                        new_ids = [x for x in all_ids if x > last_id]
                        for eid in reversed(new_ids):
                            _, msg_data = imap.fetch(str(eid).encode(), "(RFC822)")
                            for part in msg_data:
                                if isinstance(part, tuple):
                                    msg = _email_mod.message_from_bytes(part[1])
                                    frm = msg.get("From", "")
                                    if "ozon" not in frm.lower():
                                        continue
                                    body = email_svc._get_email_body(msg)
                                    subj = msg.get("Subject", "")
                                    c = email_svc._extract_ozon_code(body, subj)
                                    if c:
                                        code = c
                                        log.info("收到新验证码: %s (ID=%d)", c, eid)
                                        break
                            if code:
                                break
                    except Exception as e:
                        log.warning("邮件检查失败: %s", e)
                    if code:
                        break
                    time.sleep(3)

            if not code:
                log.error("未收到验证码")
                return False
            log.info("收到验证码: %s", code)

            # 诊断：输出验证码页面所有 input
            await asyncio.sleep(2)
            all_inputs = await self._page.query_selector_all('input')
            for inp in all_inputs:
                t = await inp.get_attribute('type') or ''
                ml = await inp.get_attribute('maxlength') or ''
                im = await inp.get_attribute('inputmode') or ''
                ac = await inp.get_attribute('autocomplete') or ''
                nm = await inp.get_attribute('name') or ''
                ph = await inp.get_attribute('placeholder') or ''
                log.info("  input type=%s maxlength=%s inputmode=%s autocomplete=%s name=%s ph=%s", t, ml, im, ac, nm, ph)

            # 重新查询验证码输入框（旧句柄可能已过期）
            await asyncio.sleep(1)
            code_input = await self._page.query_selector('input[type="text"]')
            if not code_input:
                log.error("未找到验证码输入框")
                return False
            await self._page.evaluate(
                '([sel, val]) => { const el = document.querySelector(sel); '
                'if(el){ el.value=val; el.dispatchEvent(new Event("input",{bubbles:true})); el.dispatchEvent(new Event("change",{bubbles:true})); } }',
                ['input[type="text"]', code]
            )
            log.info("验证码已填入（JS）")
            log.info("验证码填入: %s", code)

            # 等待页面导航完成
            try:
                await self._page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            await asyncio.sleep(2)
            log.info("验证码后 URL: %s", self._page.url)
            # 诊断：输出页面上的可见文字元素
            visible = await self._page.evaluate("""
                () => {
                    const res = [];
                    document.querySelectorAll('button,a,span,div').forEach(el => {
                        const t = el.textContent.trim();
                        if (t && t.length > 1 && t.length < 40) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0)
                                res.push(el.tagName + ':' + t);
                        }
                    });
                    return [...new Set(res)].slice(0, 20);
                }
            """)
            log.info("OTP/signin 页可见元素: %s", visible)
            await self._page.screenshot(path="/tmp/seller_after_code.png")

            # 检测并点击「下一步」
            for _ in range(10):
                content = await self._page.content()
                if "下一步" in content or "Далее" in content:
                    # 优先点 BUTTON，再找最小的包含「下一步」的元素
                    clicked = await self._page.evaluate("""
                        () => {
                            // 优先找 button
                            const btns = document.querySelectorAll('button');
                            for (const el of btns) {
                                const t = el.textContent.trim();
                                if (t === '下一步' || t === 'Далее' || t.includes('下一步') || t.includes('Далее')) {
                                    el.click();
                                    return 'BUTTON:' + (el.className || '');
                                }
                            }
                            // 再找 span/div 精确文本
                            const all = document.querySelectorAll('span, div, a');
                            let smallest = null, smallestArea = Infinity;
                            for (const el of all) {
                                const t = el.textContent.trim();
                                if (t === '下一步' || t === 'Далее') {
                                    const r = el.getBoundingClientRect();
                                    const area = r.width * r.height;
                                    if (area > 0 && area < smallestArea) { smallest = el; smallestArea = area; }
                                }
                            }
                            if (smallest) { smallest.click(); return smallest.tagName + ':' + (smallest.className || ''); }
                            return null;
                        }
                    """)
                    if clicked:
                        log.info("点击下一步: %s", clicked)
                        await asyncio.sleep(5)
                        break
                if "seller.ozon.ru/app" in self._page.url and "signin" not in self._page.url:
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

    async def fetch_variant_model(self, sku: str) -> Optional[Dict[str, Any]]:
        """
        请求 search-variant-model 获取单个 SKU 的尺寸/重量。
        返回 {"weight": g, "depth": mm, "width": mm, "height": mm} 或 None。
        """
        if not self._page:
            return None
        try:
            status, data = await self._page_fetch(
                "https://seller.ozon.ru/api/v1/search-variant-model",
                {"limit": "10", "name": str(sku)},
            )
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
        except Exception as e:
            log.warning("fetch_variant_model error: %s", e)
            return None

    async def fetch_data_v3(self, skus: list) -> Optional[Dict[str, Any]]:
        """
        请求 data/v3 获取批量 SKU 的销售分析数据。
        返回原始 API 响应 data 字段。
        """
        if not self._page:
            return None
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
            if status != 200:
                log.warning("data/v3 status=%d", status)
                return None
            log.info("data/v3 返回 %d items", len((data or {}).get('items', [])))
            return data
        except Exception as e:
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
