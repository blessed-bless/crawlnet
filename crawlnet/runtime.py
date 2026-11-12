"""Сборка зависимостей: Redis, очередь, хранилище, каталог спайдеров."""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from redis.asyncio import Redis

from .config import Settings, settings
from .frontier import Frontier
from .spec import SpiderSpec
from .storage import Storage


@dataclass
class Runtime:
    redis: Redis
    frontier: Frontier
    storage: Storage
    settings: Settings

    def spider_files(self) -> dict[str, Path]:
        d = self.settings.spiders_dir
        return {p.stem: p for p in sorted(d.glob("*.y*ml"))} if d.is_dir() else {}

    def load_spider(self, name: str, *, allow_path: bool = False) -> SpiderSpec:
        """По имени из каталога. Произвольный путь — только для CLI (allow_path=True)."""
        if allow_path and Path(name).is_file():
            return SpiderSpec.from_file(name)
        files = self.spider_files()
        if name not in files:
            raise KeyError(f"спайдер «{name}» не найден в каталоге")
        return SpiderSpec.from_file(files[name])

    async def export(self, job_id: str, fmt: str) -> tuple[bytes, str, str]:
        from .storage import EXPORTERS

        job = await self.frontier.job(job_id)
        if job is None:
            raise KeyError(job_id)
        fn, mime = EXPORTERS[fmt]
        rows = await self.storage.fetch(job_id=job_id)
        return fn(rows), mime, f"{job['spider']}-{job_id}.{fmt}"


@asynccontextmanager
async def open_runtime(cfg: Settings = settings, *, local: bool = False):
    """local=True — Redis в памяти процесса (fakeredis): для демо без инфраструктуры."""
    if local:
        from fakeredis import FakeAsyncRedis

        redis = FakeAsyncRedis()
    else:
        redis = Redis.from_url(cfg.redis_url)
    storage = Storage(cfg.db_url)
    await storage.init()
    try:
        yield Runtime(redis, Frontier(redis, cfg.prefix, cfg.inflight_timeout), storage, cfg)
    finally:
        await storage.close()
        await redis.aclose()
