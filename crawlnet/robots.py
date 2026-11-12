"""robots.txt: кэш в Redis (общий для воркеров) + разобранные парсеры в памяти."""
from __future__ import annotations

import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from redis.asyncio import Redis

TTL = 3600
_ALLOW_ALL = ""
_DENY_ALL = "User-agent: *\nDisallow: /"


class RobotsCache:
    def __init__(self, redis: Redis, client: httpx.AsyncClient, prefix: str = "cn"):
        self.r, self.client, self.p = redis, client, prefix
        self._local: dict[str, tuple[float, RobotFileParser]] = {}

    async def _parser(self, origin: str, user_agent: str) -> RobotFileParser:
        hit = self._local.get(origin)
        if hit and hit[0] > time.time():
            return hit[1]
        key = f"{self.p}:robots:{origin}"
        text = await self.r.get(key)
        if text is None:
            text = await self._download(origin, user_agent)
            await self.r.set(key, text, ex=TTL)
        elif isinstance(text, bytes):
            text = text.decode()
        rp = RobotFileParser()
        rp.parse(text.splitlines())
        self._local[origin] = (time.time() + TTL, rp)
        return rp

    async def _download(self, origin: str, user_agent: str) -> str:
        try:
            resp = await self.client.get(f"{origin}/robots.txt", headers={"User-Agent": user_agent}, timeout=10)
        except httpx.HTTPError:
            return _ALLOW_ALL
        if resp.status_code in (401, 403):
            return _DENY_ALL  # как в RFC 9309: доступ закрыт — не ходим
        if resp.status_code >= 400:
            return _ALLOW_ALL
        return resp.text

    async def check(self, url: str, user_agent: str) -> tuple[bool, float | None]:
        """(можно ли качать, Crawl-delay если задан)."""
        parts = urlsplit(url)
        rp = await self._parser(f"{parts.scheme}://{parts.netloc}", user_agent)
        delay = rp.crawl_delay(user_agent)
        return rp.can_fetch(user_agent, url), float(delay) if delay else None
