"""Воркер: N корутин тянут запросы из общей очереди, качают, парсят, складывают."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import socket
import time
from email.utils import parsedate_to_datetime

import httpx
from parsel import Selector

from .frontier import RUNNING, Frontier, Request
from .robots import RobotsCache
from .spec import SpiderSpec, extract_items, extract_links
from .storage import Storage
from .urls import canonicalize, host_of

log = logging.getLogger("crawlnet.worker")

RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}
HTML_TYPES = ("text/html", "application/xhtml")


def backoff(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    """Экспоненциальная задержка с «полным джиттером»."""
    return random.uniform(0, min(cap, base * 2**attempt))


def retry_after(resp: httpx.Response) -> float | None:
    v = resp.headers.get("retry-after")
    if not v:
        return None
    if v.isdigit():
        return float(v)
    with contextlib.suppress(TypeError, ValueError):
        return max(0.0, parsedate_to_datetime(v).timestamp() - time.time())
    return None


class Worker:
    def __init__(
        self,
        frontier: Frontier,
        storage: Storage,
        concurrency: int = 8,
        client: httpx.AsyncClient | None = None,
        worker_id: str | None = None,
    ):
        self.f = frontier
        self.storage = storage
        self.concurrency = concurrency
        self.client = client or httpx.AsyncClient(follow_redirects=True, http2=False)
        self.robots = RobotsCache(frontier.r, self.client, frontier.p)
        self.id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self.processed = 0
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def aclose(self) -> None:
        await self.client.aclose()

    async def run(self, until_idle: bool = False) -> None:
        """until_idle=True — выйти, когда очередь опустеет (локальный режим и тесты)."""
        service = [asyncio.create_task(self._housekeeping())]
        slots = [asyncio.create_task(self._loop(until_idle)) for _ in range(self.concurrency)]
        try:
            await asyncio.gather(*slots)
        finally:
            self._stop.set()
            for t in service:
                t.cancel()
            await asyncio.gather(*service, return_exceptions=True)

    async def _housekeeping(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                await self.f.heartbeat(self.id, {"concurrency": self.concurrency, "processed": self.processed})
                if moved := await self.f.reap():
                    log.warning("reaper вернул в очередь %d зависших запросов", moved)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=5)

    async def _loop(self, until_idle: bool) -> None:
        idle_since: float | None = None
        while not self._stop.is_set():
            req = await self.f.pop()
            if req is None:
                if until_idle and await self.f.is_idle():
                    idle_since = idle_since or time.monotonic()
                    if time.monotonic() - idle_since > 0.5:
                        return
                else:
                    idle_since = None
                wait = await self.f.next_ready_in()
                await asyncio.sleep(min(wait if wait is not None else 0.5, 0.5) + random.uniform(0, 0.05))
                continue
            idle_since = None
            try:
                await self.process(req)
            except Exception as e:  # воркер не должен падать из-за одной страницы
                log.exception("ошибка при обработке %s", req.url)
                await self.f.fail(req, f"internal: {e!r}")
            self.processed += 1

    async def process(self, req: Request) -> None:
        spec = await self.f.spec(req.job_id)
        if spec is None or await self.f.status(req.job_id) != RUNNING:
            await self.f.drop(req)
            return
        s = spec.settings

        delay = s.delay
        if s.obey_robots:
            allowed, crawl_delay = await self.robots.check(req.url, s.user_agent)
            if not allowed:
                log.info("robots.txt запрещает %s", req.url)
                await self.f.drop(req)
                return
            if crawl_delay:
                delay = max(delay, crawl_delay)

        wait = await self.f.acquire_slot(host_of(req.url), delay)
        if wait > 0:
            await self.f.defer(req, time.time() + wait)
            return

        try:
            resp = await self.client.get(req.url, headers={"User-Agent": s.user_agent}, timeout=s.timeout)
        except httpx.HTTPError as e:
            await self._retry_or_fail(req, spec, f"{type(e).__name__}: {e}")
            return

        if resp.status_code in RETRY_STATUSES:
            await self._retry_or_fail(req, spec, f"HTTP {resp.status_code}", retry_after(resp))
            return
        if resp.status_code >= 400:
            await self.f.fail(req, f"HTTP {resp.status_code}")
            return

        await self.f.mark_fetched(req.job_id, len(resp.content))
        if not resp.headers.get("content-type", "text/html").startswith(HTML_TYPES):
            await self.f.drop(req)
            return

        await self.handle_page(req, spec, str(resp.url), resp.text)
        await self.f.ack(req)

    async def handle_page(self, req: Request, spec: SpiderSpec, url: str, html: str) -> None:
        sel = Selector(text=html)
        page = spec.pages[req.page]

        if page.item:
            records, bad = extract_items(sel, page.item)
            for data in records:
                key = str(data.get(page.item.key) or url) if page.item.key else url
                await self.storage.save_item(spec.name, req.job_id, url, key, data)
            if records:
                await self.f.incr(req.job_id, "items", len(records))
            if bad:
                await self.f.incr(req.job_id, "errors", bad)
                log.info("%s: %d записей без обязательных полей", url, bad)

        if req.depth >= spec.settings.max_depth:
            return
        for rule in page.follow:
            for link in extract_links(sel, rule):
                nxt = canonicalize(link, base=url)
                if nxt and spec.domain_allowed(host_of(nxt).split(":")[0]):
                    await self.f.enqueue(req.job_id, nxt, rule.page, req.depth + 1, delay=-rule.priority)

    async def _retry_or_fail(self, req: Request, spec: SpiderSpec, reason: str, after: float | None = None) -> None:
        if req.attempt < spec.settings.max_retries:
            delay = after if after is not None else backoff(req.attempt)
            log.info("ретрай %s через %.1fс (%s)", req.url, delay, reason)
            await self.f.retry(req, delay)
        else:
            await self.f.fail(req, reason)
