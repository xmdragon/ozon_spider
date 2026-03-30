import asyncio
import json
import logging
import shutil
import socket
import tempfile
import time
from pathlib import Path
from typing import Any

from chrome_launcher import kill, start_chrome, tiled_window_geometry
from spider import fetch_product, load_cookies, save_cookies, setup_page

log = logging.getLogger(__name__)

SPIDER_RUNTIME_ROOT = Path("/tmp/ozon_spider_runtime")
SPIDER_STATE_PATH = SPIDER_RUNTIME_ROOT / "profile_state.json"
SPIDER_COOKIES_PATH = SPIDER_RUNTIME_ROOT / "cookies.json"


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class SpiderWorker:
    def __init__(
        self,
        playwright,
        chrome_bin: str,
        display: str,
        user_data_dir: Path | None = None,
        persistent: bool = False,
        cookies_path: Path | None = None,
        window_slot: int = 0,
    ):
        self._playwright = playwright
        self._chrome_bin = chrome_bin
        self._display = display

        self.cdp_port = _pick_free_port()
        self.user_data_dir = user_data_dir or Path(tempfile.mkdtemp(prefix="ozon_spider_profile_"))
        self.persistent = persistent
        self.cookies_path = cookies_path
        self.window_slot = window_slot
        self.chrome_proc = None
        self.browser = None
        self.context = None
        self.page = None
        self.last_used = time.time()

    async def start(self):
        window_size, window_position = tiled_window_geometry(self.window_slot)
        self.chrome_proc = start_chrome(
            self._chrome_bin,
            self.cdp_port,
            self._display,
            user_data_dir=str(self.user_data_dir) if self.user_data_dir else None,
            window_size=window_size,
            window_position=window_position,
        )
        self.browser = await self._playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self.cdp_port}"
        )
        self.context = (
            self.browser.contexts[0]
            if self.browser.contexts
            else await self.browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow")
        )
        if self.cookies_path:
            await load_cookies(self.context, path=str(self.cookies_path))
        self.page = await setup_page(self.context)
        self.last_used = time.time()
        log.info(
            "Spider worker ready on port %d (%s)%s",
            self.cdp_port,
            self.user_data_dir,
            " [persistent]" if self.persistent else "",
        )
        return self

    async def fetch(self, sku: str) -> tuple[dict | None, str]:
        self.last_used = time.time()
        data, status = await fetch_product(self.page, sku)
        if status == "ok":
            try:
                if self.cookies_path:
                    await save_cookies(self.context, path=str(self.cookies_path))
            except Exception:
                pass
        return data, status

    def is_ready(self) -> bool:
        try:
            return self.page is not None and not self.page.is_closed()
        except Exception:
            return False

    async def close(self, delete_profile: bool = True):
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
        if delete_profile and self.user_data_dir:
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
        self._spawn_seq = 0
        SPIDER_RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        self._persistent_profile_dir = self._load_persistent_profile_dir()

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
            worker = await self._new_worker().start()
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

    async def release(
        self,
        worker: SpiderWorker,
        unhealthy: bool = False,
        successful: bool = False,
        reset_pool: bool = False,
    ):
        if worker is None:
            return

        workers_to_close: list[SpiderWorker] = []
        async with self._cond:
            self._remove_from_list(self._in_use, worker)
            self._prune_dead_locked()
            if successful:
                self._promote_persistent_locked(worker)
            if reset_pool:
                workers_to_close = list(self._workers)
                self._workers.clear()
                self._available.clear()
                self._in_use.clear()
                self._clear_persistent_locked(delete_dir=False)
            elif unhealthy or not worker.is_ready():
                if self._is_persistent_worker(worker):
                    self._clear_persistent_locked(delete_dir=True)
                self._remove_worker_locked(worker)
                workers_to_close = [worker]
            elif worker in self._workers and worker not in self._available:
                worker.last_used = time.time()
                self._available.append(worker)
            self._cond.notify_all()

        if workers_to_close:
            seen = set()
            unique_workers = []
            for item in workers_to_close:
                ident = id(item)
                if ident in seen:
                    continue
                seen.add(ident)
                unique_workers.append(item)
            for item in unique_workers:
                await item.close(delete_profile=True)
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
            await worker.close(delete_profile=not self._is_persistent_worker(worker))

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
                worker = await self._new_worker().start()
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
                    await worker.close(delete_profile=not self._is_persistent_worker(worker))
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

    def _load_persistent_profile_dir(self) -> Path | None:
        try:
            if not SPIDER_STATE_PATH.exists():
                return None
            payload = json.loads(SPIDER_STATE_PATH.read_text(encoding="utf-8"))
            raw = payload.get("persistent_profile_dir")
            if not raw:
                return None
            path = Path(raw)
            return path if path.exists() else None
        except Exception:
            return None

    def _write_persistent_profile_dir(self, path: Path | None):
        payload = {
            "persistent_profile_dir": str(path) if path else None,
            "updated_at": time.time(),
        }
        SPIDER_STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _is_persistent_worker(self, worker: SpiderWorker) -> bool:
        return (
            self._persistent_profile_dir is not None
            and worker.user_data_dir is not None
            and worker.user_data_dir == self._persistent_profile_dir
        )

    def _promote_persistent_locked(self, worker: SpiderWorker):
        if self._persistent_profile_dir is None and worker.user_data_dir is not None:
            self._persistent_profile_dir = worker.user_data_dir
            worker.persistent = True
            self._write_persistent_profile_dir(self._persistent_profile_dir)
            log.info("Promoted spider profile to persistent: %s", self._persistent_profile_dir)

    def _clear_persistent_locked(self, delete_dir: bool):
        path = self._persistent_profile_dir
        self._persistent_profile_dir = None
        self._write_persistent_profile_dir(None)
        try:
            SPIDER_COOKIES_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        if delete_dir and path:
            try:
                shutil.rmtree(path, ignore_errors=True)
            except Exception:
                pass

    def _new_worker(self) -> SpiderWorker:
        use_persistent = self._persistent_profile_dir is not None and not any(
            worker.user_data_dir == self._persistent_profile_dir for worker in self._workers
        )
        profile_dir = self._persistent_profile_dir if use_persistent else None
        window_slot = 2 * (self._spawn_seq % 2)
        self._spawn_seq += 1
        return SpiderWorker(
            self._playwright,
            self._chrome_bin,
            self._display,
            user_data_dir=profile_dir,
            persistent=use_persistent,
            cookies_path=SPIDER_COOKIES_PATH,
            window_slot=window_slot,
        )

    @staticmethod
    def _remove_from_list(items: list, value):
        try:
            items.remove(value)
        except ValueError:
            pass
