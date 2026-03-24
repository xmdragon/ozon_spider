"""
Ozon Spider HTTP 服务。

启动时初始化两个 Chrome 实例：
- Spider Chrome（端口 9223）：抓取商品页面数据
- Seller Chrome（端口 9224）：已登录 seller session，提供尺寸/重量 API

接口：
  GET  /health               服务状态
  GET  /sku?sku=xxx          抓取商品数据（含尺寸重量）
  POST /variant-model        批量查尺寸/重量 {"skus": [...]}
  POST /data-v3              批量销售数据   {"skus": [...]}
"""
import asyncio
import logging
import os
import random
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, Query, HTTPException
from pydantic import BaseModel

from config import (
    CHROME_BIN, CDP_PORT, XVFB_DISPLAY,
    SELLER_EMAIL, SELLER_EMAIL_APP_PASSWORD, SELLER_CLIENT_ID, SELLER_STORAGE_STATE,
)
from chrome_launcher import start_chrome, kill, start_xvfb
from spider import fetch_product, setup_page, load_cookies
from seller_login import SellerSession, SELLER_CDP_PORT
from playwright.async_api import async_playwright

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

# ─── 全局状态 ───────────────────────────────────────────────────────────────

class AppState:
    xvfb_proc = None
    spider_chrome_proc = None
    seller_session: Optional[SellerSession] = None
    spider_playwright = None
    spider_browser = None
    spider_context = None
    spider_page = None
    _lock: asyncio.Lock = None

    def __init__(self):
        self._lock = asyncio.Lock()

state = AppState()


# ─── Lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动 Xvfb
    os.environ["DISPLAY"] = XVFB_DISPLAY
    try:
        state.xvfb_proc = start_xvfb(XVFB_DISPLAY)
    except Exception as e:
        log.warning("Xvfb start failed (may already be running): %s", e)

    # 启动 Spider Chrome
    try:
        state.spider_chrome_proc = start_chrome(CHROME_BIN, CDP_PORT, XVFB_DISPLAY)
        log.info("Spider Chrome started on port %d", CDP_PORT)
    except Exception as e:
        log.error("Spider Chrome failed: %s", e)

    # 初始化 spider playwright
    try:
        state.spider_playwright = await async_playwright().start()
        state.spider_browser = await state.spider_playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{CDP_PORT}"
        )
        state.spider_context = (
            state.spider_browser.contexts[0]
            if state.spider_browser.contexts
            else await state.spider_browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow")
        )
        await load_cookies(state.spider_context)
        state.spider_page = await setup_page(state.spider_context)
        log.info("Spider browser ready")
    except Exception as e:
        log.error("Spider browser init failed: %s", e)

    # 启动 Seller session（后台，不阻塞启动）
    async def _init_seller():
        try:
            session = SellerSession(
                SELLER_EMAIL, SELLER_EMAIL_APP_PASSWORD,
                SELLER_CLIENT_ID, SELLER_STORAGE_STATE,
            )
            ok = await session.start()
            if ok:
                state.seller_session = session
                log.info("Seller session ready, URL: %s", session.page.url)
            else:
                log.warning("Seller session login failed — dimensions unavailable")
        except Exception as e:
            log.error("Seller session init error: %s", e)

    asyncio.create_task(_init_seller())

    yield

    # 关闭
    if state.seller_session:
        await state.seller_session.close()
    if state.spider_page:
        try: await state.spider_page.close()
        except Exception: pass
    if state.spider_context:
        try: await state.spider_context.close()
        except Exception: pass
    if state.spider_playwright:
        await state.spider_playwright.stop()
    if state.spider_chrome_proc:
        kill(state.spider_chrome_proc)
    if state.xvfb_proc:
        kill(state.xvfb_proc)


app = FastAPI(title="Ozon Spider API", lifespan=lifespan)


# ─── 路由 ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "spider_ready": state.spider_page is not None,
        "seller_ready": state.seller_session is not None,
    }


@app.get("/sku")
async def get_sku(sku: str = Query(..., description="Ozon SKU")):
    """
    抓取商品完整数据，自动补充尺寸重量（如果 seller session 就绪）。
    """
    if state.spider_page is None:
        raise HTTPException(503, "Spider browser not ready")

    async with state._lock:
        try:
            data = await fetch_product(state.spider_page, sku)
        except Exception as e:
            log.error("fetch_product error SKU %s: %s", sku, e)
            raise HTTPException(500, str(e))

    if not data:
        raise HTTPException(404, f"Product {sku} not found or blocked")

    # 补充尺寸重量
    if state.seller_session:
        try:
            dims = await state.seller_session.fetch_variant_model(sku)
            if dims:
                data["dimensions"] = dims
        except Exception as e:
            log.warning("fetch_variant_model error SKU %s: %s", sku, e)

    return data


class SkuListRequest(BaseModel):
    skus: List[str]


@app.post("/variant-model")
async def variant_model(req: SkuListRequest):
    """
    批量查询 SKU 尺寸/重量。
    返回 {sku: {weight, depth, width, height}}
    """
    if not state.seller_session:
        raise HTTPException(503, "Seller session not ready")
    results = {}
    for sku in req.skus:
        dims = await state.seller_session.fetch_variant_model(sku)
        if dims:
            results[sku] = dims
    return {"dimensions": results, "total": len(results)}


@app.post("/data-v3")
async def data_v3(req: SkuListRequest):
    """
    批量查询 SKU 销售分析数据（seller data/v3）。
    """
    if not state.seller_session:
        raise HTTPException(503, "Seller session not ready")
    result = await state.seller_session.fetch_data_v3(req.skus)
    return {"data": result}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8765, reload=False)
