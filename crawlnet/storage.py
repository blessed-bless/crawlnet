"""Хранилище результатов (SQLite локально, PostgreSQL в docker) и экспорт."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, UniqueConstraint, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

metadata = MetaData()

items = Table(
    "items",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("spider", String(64), nullable=False, index=True),
    Column("key", String(512), nullable=False),
    Column("job_id", String(32), nullable=False, index=True),
    Column("url", String(2048), nullable=False),
    Column("data", JSON, nullable=False),
    Column("data_hash", String(40), nullable=False),
    Column("versions", Integer, nullable=False, default=1),
    Column("first_seen", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("spider", "key", name="uq_items_spider_key"),
)


class Storage:
    def __init__(self, url: str):
        self.engine: AsyncEngine = create_async_engine(url)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    def _insert(self):
        if self.engine.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        return insert

    async def save_item(self, spider: str, job_id: str, url: str, key: str, data: dict[str, Any]) -> None:
        """Upsert по (spider, key). Если данные изменились — растёт счётчик версий."""
        now = datetime.now(UTC)
        h = hashlib.sha1(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        insert = self._insert()
        stmt = insert(items).values(
            spider=spider, key=key[:512], job_id=job_id, url=url, data=data, data_hash=h,
            versions=1, first_seen=now, updated_at=now,
        )
        ex = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=["spider", "key"],
            set_={
                "job_id": ex.job_id, "url": ex.url, "data": ex.data, "data_hash": ex.data_hash, "updated_at": ex.updated_at,
                "versions": items.c.versions + (items.c.data_hash != ex.data_hash).cast(Integer),
            },
        )
        async with self.engine.begin() as conn:
            await conn.execute(stmt)

    async def fetch(self, *, job_id: str | None = None, spider: str | None = None, limit: int | None = None) -> list[dict]:
        q = select(items).order_by(items.c.id)
        if job_id:
            q = q.where(items.c.job_id == job_id)
        if spider:
            q = q.where(items.c.spider == spider)
        if limit:
            q = q.limit(limit)
        async with self.engine.connect() as conn:
            return [dict(r._mapping) for r in await conn.execute(q)]

    async def count(self, job_id: str) -> int:
        async with self.engine.connect() as conn:
            return (await conn.execute(select(func.count()).where(items.c.job_id == job_id))).scalar_one()


def _flatten(rows: list[dict]) -> tuple[list[str], list[list[Any]]]:
    fields: dict[str, None] = {}
    for r in rows:
        fields.update(dict.fromkeys(r["data"]))
    header = ["url", *fields, "versions", "updated_at"]
    out = []
    for r in rows:
        vals = [r["url"]]
        for f in fields:
            v = r["data"].get(f)
            vals.append("; ".join(map(str, v)) if isinstance(v, list) else v)
        vals += [r["versions"], r["updated_at"].replace(tzinfo=None) if r["updated_at"] else None]
        out.append(vals)
    return header, out


def to_csv(rows: list[dict]) -> bytes:
    header, data = _flatten(rows)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(data)
    return buf.getvalue().encode("utf-8-sig")  # BOM — чтобы Excel открыл кириллицу


def to_xlsx(rows: list[dict], title: str = "items") -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    header, data = _flatten(rows)
    wb = Workbook()
    ws = wb.active
    ws.title = title[:31]
    ws.append(header)
    for row in data:
        ws.append(row)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="2F4858")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for i, col in enumerate(header, 1):
        width = max([len(str(col))] + [len(str(r[i - 1] or "")) for r in data[:200]])
        ws.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 8), 60)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


EXPORTERS = {"csv": (to_csv, "text/csv"), "xlsx": (to_xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
