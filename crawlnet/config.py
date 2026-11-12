import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    redis_url: str = field(default_factory=lambda: _env("CRAWLNET_REDIS_URL", "redis://localhost:6379/0"))
    db_url: str = field(default_factory=lambda: _env("CRAWLNET_DB_URL", "sqlite+aiosqlite:///crawlnet.db"))
    spiders_dir: Path = field(default_factory=lambda: Path(_env("CRAWLNET_SPIDERS_DIR", "spiders")))
    prefix: str = field(default_factory=lambda: _env("CRAWLNET_PREFIX", "cn"))
    tg_token: str = field(default_factory=lambda: _env("CRAWLNET_TG_TOKEN", ""))
    tg_allowed: frozenset[int] = field(
        default_factory=lambda: frozenset(
            int(x) for x in _env("CRAWLNET_TG_ALLOWED", "").replace(" ", "").split(",") if x
        )
    )
    # сколько секунд запрос может висеть «в работе», прежде чем его заберёт другой воркер
    inflight_timeout: float = field(default_factory=lambda: float(_env("CRAWLNET_INFLIGHT_TIMEOUT", "120")))


settings = Settings()
