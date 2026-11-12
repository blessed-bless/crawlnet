"""Декларативное описание спайдера (YAML) и извлечение полей."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from parsel import Selector
from pydantic import BaseModel, Field, field_validator, model_validator


class FieldSpec(BaseModel):
    css: str | None = None
    xpath: str | None = None
    regex: str | None = None
    type: Literal["str", "int", "float", "bool"] = "str"
    many: bool = False
    default: Any = None
    map: dict[str, Any] | None = None
    join: str | None = None  # склеить все совпадения в одну строку

    @model_validator(mode="after")
    def _one_selector(self) -> FieldSpec:
        if bool(self.css) == bool(self.xpath):
            raise ValueError("нужен ровно один из css / xpath")
        if self.regex:
            re.compile(self.regex)
        return self


class FollowRule(BaseModel):
    css: str | None = None
    xpath: str | None = None
    page: str
    pattern: str | None = None  # фильтр URL по регулярке
    priority: int = 0

    @model_validator(mode="after")
    def _one_selector(self) -> FollowRule:
        if bool(self.css) == bool(self.xpath):
            raise ValueError("нужен ровно один из css / xpath")
        return self


class ItemSpec(BaseModel):
    each: str | None = None  # CSS-блок, если записей на странице несколько; поля ищутся внутри него
    key: str | None = None  # поле-идентификатор для upsert; по умолчанию URL
    required: list[str] = Field(default_factory=list)
    fields: dict[str, FieldSpec]

    @model_validator(mode="after")
    def _key_known(self) -> ItemSpec:
        unknown = [k for k in [self.key, *self.required] if k and k not in self.fields]
        if unknown:
            raise ValueError(f"key/required ссылаются на несуществующие поля: {unknown}")
        if self.each and not self.key:
            raise ValueError("при each нужен key — иначе записи с одной страницы затрут друг друга")
        return self


class PageSpec(BaseModel):
    follow: list[FollowRule] = Field(default_factory=list)
    item: ItemSpec | None = None


class StartUrl(BaseModel):
    url: str
    page: str


class SpiderSettings(BaseModel):
    delay: float = 1.0  # минимальный интервал между запросами к одному домену, сек
    max_pages: int = 1000
    max_depth: int = 10
    max_retries: int = 3
    timeout: float = 20.0
    obey_robots: bool = True
    user_agent: str = "crawlnet/0.1 (+https://github.com/)"


class SpiderSpec(BaseModel):
    name: str
    allowed_domains: list[str] = Field(default_factory=list)
    start: list[StartUrl]
    pages: dict[str, PageSpec]
    settings: SpiderSettings = Field(default_factory=SpiderSettings)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9_-]{1,64}", v):
            raise ValueError("имя спайдера: a-z, 0-9, _ и -")
        return v

    @model_validator(mode="after")
    def _pages_exist(self) -> SpiderSpec:
        refs = [s.page for s in self.start] + [f.page for p in self.pages.values() for f in p.follow]
        missing = sorted(set(refs) - self.pages.keys())
        if missing:
            raise ValueError(f"неизвестные типы страниц: {missing}")
        return self

    def domain_allowed(self, host: str) -> bool:
        if not self.allowed_domains:
            return True
        host = host.lower()
        return any(host == d or host.endswith("." + d) for d in self.allowed_domains)

    @classmethod
    def from_yaml(cls, text: str) -> SpiderSpec:
        return cls.model_validate(yaml.safe_load(text))

    @classmethod
    def from_file(cls, path: str | Path) -> SpiderSpec:
        return cls.from_yaml(Path(path).read_text(encoding="utf-8"))


_CASTS = {"str": str, "int": int, "float": float, "bool": lambda v: str(v).strip().lower() in {"1", "true", "yes", "да"}}


def _convert(raw: str, spec: FieldSpec) -> Any:
    value: Any = raw.strip()
    if spec.regex:
        m = re.search(spec.regex, value)
        if not m:
            return None
        value = m.group(1) if m.groups() else m.group(0)
    if spec.map is not None:
        value = spec.map.get(value, value)
    if spec.type == "str":
        return value
    try:
        if spec.type in ("int", "float"):
            value = str(value).replace(" ", "").replace(" ", "").replace(",", ".")
            return int(float(value)) if spec.type == "int" else float(value)
        return _CASTS[spec.type](value)
    except (ValueError, TypeError):
        return None


def extract_field(sel: Selector, spec: FieldSpec) -> Any:
    nodes = sel.css(spec.css) if spec.css else sel.xpath(spec.xpath)
    values = [v for v in (_convert(r, spec) for r in nodes.getall()) if v not in (None, "")]
    if spec.join is not None:
        return spec.join.join(map(str, values)) if values else spec.default
    if spec.many:
        return values
    return values[0] if values else spec.default


class ItemValidationError(ValueError):
    pass


def extract_item(sel: Selector, item: ItemSpec) -> dict[str, Any]:
    data = {name: extract_field(sel, f) for name, f in item.fields.items()}
    missing = [k for k in item.required if data.get(k) in (None, "", [])]
    if missing:
        raise ItemValidationError(f"нет обязательных полей: {missing}")
    return data


def extract_items(sel: Selector, item: ItemSpec) -> tuple[list[dict[str, Any]], int]:
    """Все записи страницы и число отброшенных невалидных."""
    blocks = sel.css(item.each) if item.each else [sel]
    out, bad = [], 0
    for b in blocks:
        try:
            out.append(extract_item(b, item))
        except ItemValidationError:
            bad += 1
    return out, bad


def extract_links(sel: Selector, rule: FollowRule) -> list[str]:
    nodes = sel.css(rule.css) if rule.css else sel.xpath(rule.xpath)
    links = [x.strip() for x in nodes.getall() if x and x.strip()]
    if rule.pattern:
        rx = re.compile(rule.pattern)
        links = [x for x in links if rx.search(x)]
    return links
