import asyncio
import logging
import shutil
import socket
import tempfile
import time
from pathlib import Path
from typing import Any

from chrome_launcher import kill, start_chrome
from spider import fetch_product, load_cookies, save_cookies, setup_page

log = logging.getLogger(__name__)


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class SpiderWorker:
    def __init__(self, playwright, chrome_bin: str, display: str):
        self._playwright = playwright
        self._chrome_bin = chrome_bin
        self._display = display

        self.cdp_port = _pick_free_port()
        self.user_data_dir = Path(tempfile.mkdtemp(prefix="ozon_spider_profile_"))
        self.chrome_proc = None
        self.browser = None
        self.context = None
        self.page = None
        self.last_used = time.time()

    async def start(self):
        self.chrome_proc = start_chrome(
            self._chrome_bin,
            self.cdp_port,
            self._display,
            user_data_dir=str(self.user_data_dir),
        )
        self.browser = await self._playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self.cdp_port}"
        )
        self.context = (
            self.browser.contexts[0]
            if self.browser.contexts
            else await self.browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow")
        )
        await load_cookies(self.context)
        self.page = await setup_page(self.context)
        self.last_used = time.time()
        log.info("Spider worker ready on port %d (%s)", self.cdp_port, self.user_data_dir)
        return self

    async def fetch(self, sku: str) -> tuple[dict | None, str]:
        self.last_used = time.time()
        data, status = await fetch_product(self.page, sku)
        if status == "ok":
            try:
                await save_cookies(self.context)
            except Exception:
                pass
        return data, status

    def is_ready(self) -> bool:
        try:
            return self.page is not None and not self.page.is_closed()
        except Exception:
            return False

    async def close(self):
        try:
            if self.page and not self.page.is_closed():
                await self.page.close()
        except Exception:
            pass
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        if self.chrome_proc:
            kill(self.chrome_proc)
        try:
            shutil.rmtree(self.user_data_dir, ignore_errors=True)
        except Exception:
            pass


class SpiderWorkerPool:
    def __init__(
        self,
        playwright,
        chrome_bin: str,
        display: str,
        min_workers: int = 1,
        max_workers: int = 3,
        idle_ttl_seconds: int = 90,
    ):
        self._playwright = playwright
        self._chrome_bin = chrome_bin
        self._display = display
        self.min_workers = min_workers
        self.max_workers = max_workers
        self.idle_ttl_seconds = idle_ttl_seconds

        self._workers: list[SpiderWorker] = []
        self._available: list[SpiderWorker] = []
        self._in_use: list[SpiderWorker] = []
        self._creating = 0
        self._closed = False
        self._cond = asyncio.Condition()
        self._reaper_task: asyncio.Task | None = None
        self._fill_task: asyncio.Task | None = None

    async def start(self):
        await self._ensure_min_workers()
        self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def acquire(self) -> SpiderWorker:
        while True:
            async with self._cond:
                self._prune_dead_locked()
                if self._available:
                    worker = self._available.pop()
                    self._in_use.append(worker)
                    worker.last_used = time.time()
                    return worker

                if len(self._workers) + self._creating < self.max_workers:
                    self._creating += 1
                    break

                await self._cond.wait()

        try:
            worker = await SpiderWorker(self._playwright, self._chrome_bin, self._display).start()
        except Exception:
            async with self._cond:
                self._creating -= 1
                self._cond.notify_all()
            raise

        async with self._cond:
            self._creating -= 1
            if self._closed:
                self._cond.notify_all()
                await worker.close()
                raise RuntimeError("spider worker pool is closed")
            self._workers.append(worker)
            self._in_use.append(worker)
            self._cond.notify_all()
            log.info("Spider worker pool scaled up: %d/%d", len(self._workers), self.max_workers)
            return worker

    async def release(self, worker: SpiderWorker, unhealthy: bool = False):
        if worker is None:
            return

        should_close = False
        async with self._cond:
            self._remove_from_list(self._in_use, worker)
            self._prune_dead_locked()
            if unhealthy or not worker.is_ready():
                self._remove_worker_locked(worker)
                should_close = True
            elif worker in self._workers and worker not in self._available:
                worker.last_used = time.time()
                self._available.append(worker)
            self._cond.notify_all()

        if should_close:
            await worker.close()
            self._schedule_fill()

    async def close(self):
        self._closed = True
        for task in (self._reaper_task, self._fill_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        async with self._cond:
            workers = list(self._workers)
            self._workers.clear()
            self._available.clear()
            self._in_use.clear()
            self._creating = 0
            self._cond.notify_all()

        for worker in workers:
            await worker.close()

    async def stats(self) -> dict:
        async with self._cond:
            self._prune_dead_locked()
            return {
                "ready": len(self._workers) > 0,
                "total_workers": len(self._workers),
                "available_workers": len(self._available),
                "in_use_workers": len(self._in_use),
                "creating_workers": self._creating,
                "min_workers": self.min_workers,
                "max_workers": self.max_workers,
            }

    async def _ensure_min_workers(self):
        while True:
            async with self._cond:
                self._prune_dead_locked()
                if self._closed or len(self._workers) + self._creating >= self.min_workers:
                    return
                self._creating += 1

            try:
                worker = await SpiderWorker(self._playwright, self._chrome_bin, self._display).start()
            except Exception as e:
                log.warning("spider worker create failed: %s", e)
                async with self._cond:
                    self._creating -= 1
                    self._cond.notify_all()
                return

            async with self._cond:
                self._creating -= 1
                if self._closed:
                    self._cond.notify_all()
                    await worker.close()
                    return
                self._workers.append(worker)
                self._available.append(worker)
                self._cond.notify_all()

    def _schedule_fill(self):
        if self._closed:
            return
        if self._fill_task and not self._fill_task.done():
            return
        self._fill_task = asyncio.create_task(self._ensure_min_workers())

    async def _reaper_loop(self):
        try:
            while not self._closed:
                await asyncio.sleep(15)
                now = time.time()
                to_close = []
                async with self._cond:
                    self._prune_dead_locked()
                    for worker in list(self._available):
                        if len(self._workers) - len(to_close) <= self.min_workers:
                            break
                        if now - worker.last_used < self.idle_ttl_seconds:
                            continue
                        self._remove_from_list(self._available, worker)
                        self._remove_worker_locked(worker)
                        to_close.append(worker)
                    self._cond.notify_all()

                for worker in to_close:
                    await worker.close()
                if to_close:
                    log.info("Spider worker pool scaled down: %d worker(s) closed", len(to_close))
        except asyncio.CancelledError:
            raise

    def _prune_dead_locked(self):
        for worker in list(self._workers):
            if worker.is_ready():
                continue
            self._remove_worker_locked(worker)

    def _remove_worker_locked(self, worker: SpiderWorker):
        self._remove_from_list(self._workers, worker)
        self._remove_from_list(self._available, worker)
        self._remove_from_list(self._in_use, worker)

    @staticmethod
    def _remove_from_list(items: list, value):
        try:
            items.remove(value)
        except ValueError:
            pass
