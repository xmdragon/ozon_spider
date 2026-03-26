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
import random
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, Query, HTTPException
from pydantic import BaseModel

from config import (
    CHROME_BIN, CDP_PORT, BROWSER_DISPLAY, BROWSER_USE_XVFB, apply_browser_display_env,
    SELLER_ACCOUNTS,
)
from chrome_launcher import kill, start_xvfb
from spider_pool import SpiderWorkerPool
from seller_login import SellerSessionManager, SellerSessionUnavailable
from playwright.async_api import async_playwright

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

SPIDER_MIN_WORKERS = 1
SPIDER_MAX_WORKERS = 2
SPIDER_IDLE_WORKER_TTL_SECONDS = 90

# ─── 全局状态 ───────────────────────────────────────────────────────────────

class AppState:
    xvfb_proc = None
    seller_manager: Optional[SellerSessionManager] = None
    spider_playwright = None
    spider_pool: Optional[SpiderWorkerPool] = None

state = AppState()


async def _fetch_with_profile_recovery(sku: str) -> tuple[dict | None, str]:
    """
    Anonymous spider fetch with strict profile hygiene.

    Rule:
    - final state is product (`status == "ok"`): keep profile/state
    - any non-`ok` final state: treat profile as polluted, close it, delete it,
      let the pool create a fresh profile, then retry once
    """
    if not state.spider_pool:
        raise HTTPException(503, "Spider page pool not ready")

    attempts = 2
    last_status = "unavailable"
    last_data = None
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        page = await state.spider_pool.acquire()
        successful = False
        unhealthy = False
        reset_pool = False
        try:
            data, fetch_status = await page.fetch(sku)
            last_data = data
            last_status = fetch_status
            successful = fetch_status == "ok"
            unhealthy = not successful
            reset_pool = unhealthy
            if successful:
                return data, fetch_status
            log.warning(
                "anonymous fetch attempt %d/%d for SKU %s ended with status=%s; resetting anonymous pool and retrying",
                attempt,
                attempts,
                sku,
                fetch_status,
            )
        except Exception as e:
            last_error = e
            unhealthy = True
            reset_pool = True
            log.error(
                "anonymous fetch attempt %d/%d for SKU %s failed: %s; resetting anonymous pool",
                attempt,
                attempts,
                sku,
                e,
            )
            if attempt == attempts:
                raise HTTPException(500, str(e))
        finally:
            await state.spider_pool.release(
                page,
                unhealthy=unhealthy,
                successful=successful,
                reset_pool=reset_pool,
            )

    if last_error:
        raise HTTPException(500, str(last_error))
    return last_data, last_status


# ─── Lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    apply_browser_display_env()
    if BROWSER_USE_XVFB:
        try:
            state.xvfb_proc = start_xvfb(BROWSER_DISPLAY)
        except Exception as e:
            log.warning("Xvfb start failed (may already be running): %s", e)
    else:
        log.info("Using native display %s", BROWSER_DISPLAY)

    # 初始化 spider worker pool
    try:
        state.spider_playwright = await async_playwright().start()
        state.spider_pool = SpiderWorkerPool(
            state.spider_playwright,
            CHROME_BIN,
            BROWSER_DISPLAY,
            min_workers=SPIDER_MIN_WORKERS,
            max_workers=SPIDER_MAX_WORKERS,
            idle_ttl_seconds=SPIDER_IDLE_WORKER_TTL_SECONDS,
        )
        await state.spider_pool.start()
        log.info("Spider worker pool ready")
    except Exception as e:
        log.error("Spider worker pool init failed: %s", e)

    # 启动 Seller session manager（后台，不阻塞启动）
    async def _init_seller():
        try:
            state.seller_manager = SellerSessionManager(SELLER_ACCOUNTS)
            await state.seller_manager.start()
            log.info("Seller session manager started")
        except Exception as e:
            log.error("Seller session init error: %s", e)

    asyncio.create_task(_init_seller())

    yield

    # 关闭
    if state.seller_manager:
        await state.seller_manager.close()
    if state.spider_pool:
        await state.spider_pool.close()
    if state.spider_playwright:
        await state.spider_playwright.stop()
    if state.xvfb_proc:
        kill(state.xvfb_proc)


app = FastAPI(title="Ozon Spider API", lifespan=lifespan)


def _normalize_dimensions_for_sku(dims: dict) -> dict:
    def normalize_number(value):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value

    return {
        "weight": normalize_number(dims.get("weight")),
        "height": normalize_number(dims.get("height")),
        "width": normalize_number(dims.get("width")),
        "length": normalize_number(dims.get("depth")),
    }


# ─── 路由 ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    seller = {"ready": False}
    if state.seller_manager:
        seller = await state.seller_manager.health_snapshot()
    spider = {"ready": False}
    if state.spider_pool:
        spider = await state.spider_pool.stats()
    return {
        "status": "ok",
        "spider_ready": spider["ready"],
        "seller_ready": seller["ready"],
        "spider": spider,
        "seller": seller,
    }


@app.get("/sku")
async def get_sku(sku: str = Query(..., description="Ozon SKU")):
    """
    抓取商品完整数据，自动补充尺寸重量（如果 seller session 就绪）。
    """
    data, fetch_status = await _fetch_with_profile_recovery(sku)

    if not data:
        if fetch_status == "blocked":
            raise HTTPException(503, f"Product {sku} blocked by upstream antibot")
        raise HTTPException(404, f"Product {sku} not found or unavailable")

    # 尺寸重量是强依赖，seller 不可用时整条失败
    if not state.seller_manager:
        raise HTTPException(503, "Seller session manager not ready")
    try:
        dims = await state.seller_manager.call_with_failover("fetch_variant_model", sku)
    except SellerSessionUnavailable as e:
        log.warning("seller unavailable for SKU %s: %s", sku, e)
        raise HTTPException(503, "Seller session unavailable")
    if not dims:
        raise HTTPException(502, f"Dimensions unavailable for SKU {sku}")
    data["dimensions"] = _normalize_dimensions_for_sku(dims)

    return data


class SkuListRequest(BaseModel):
    skus: List[str]


@app.post("/variant-model")
async def variant_model(req: SkuListRequest):
    """
    批量查询 SKU 尺寸/重量。
    返回 {sku: {weight, depth, width, height}}
    """
    if not state.seller_manager:
        raise HTTPException(503, "Seller session manager not ready")
    results = {}
    for sku in req.skus:
        try:
            dims = await state.seller_manager.call_with_failover("fetch_variant_model", sku)
        except SellerSessionUnavailable:
            raise HTTPException(503, "Seller session unavailable")
        if dims:
            results[sku] = dims
    return {"dimensions": results, "total": len(results)}


@app.post("/data-v3")
async def data_v3(req: SkuListRequest):
    """
    批量查询 SKU 销售分析数据（seller data/v3）。
    """
    if not state.seller_manager:
        raise HTTPException(503, "Seller session manager not ready")
    try:
        result = await state.seller_manager.call_with_failover("fetch_data_v3", req.skus)
    except SellerSessionUnavailable:
        raise HTTPException(503, "Seller session unavailable")
    return {"data": result}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8765, reload=False)
