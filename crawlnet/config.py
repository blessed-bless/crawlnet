from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CRAWLNET_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    redis_url: str = "redis://localhost:6379/0"
    db_url: str = "sqlite+aiosqlite:///crawlnet.db"
    spiders_dir: Path = Path("spiders")
    prefix: str = "cn"
    tg_token: str = ""
    tg_allowed: frozenset[int] = Field(default_factory=frozenset)
    # How long a claim may stay in inflight before another worker may reclaim it
    inflight_timeout: float = 120.0

    @field_validator("tg_allowed", mode="before")
    @classmethod
    def _parse_tg_allowed(cls, value: object) -> frozenset[int]:
        if value is None or value == "":
            return frozenset()
        if isinstance(value, frozenset):
            return value
        if isinstance(value, (set, list, tuple)):
            return frozenset(int(x) for x in value)
        text = str(value).replace(" ", "")
        if not text:
            return frozenset()
        return frozenset(int(part) for part in text.split(",") if part)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
