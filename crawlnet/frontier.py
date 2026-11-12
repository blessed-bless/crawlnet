"""Общая для всех воркеров очередь запросов в Redis.

Ключи (prefix = cn):
  cn:jobs              ZSET  job_id -> created_ts
  cn:job:{id}          HASH  метаданные и счётчики задачи
  cn:spec:{id}         STR   снимок спецификации спайдера (JSON)
  cn:seen:{id}         SET   отпечатки уже поставленных URL — дедуп
  cn:frontier          ZSET  запрос -> время, когда его можно брать
  cn:inflight          ZSET  запрос -> дедлайн; просроченные возвращает reaper
  cn:slot:{host}       STR   «замок» вежливости: один запрос к хосту за delay
  cn:dead:{id}         LIST  запросы, исчерпавшие ретраи
  cn:rate:{id}         HASH  минута -> число скачанных страниц
  cn:workers           HASH  worker_id -> heartbeat JSON
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, replace

from redis.asyncio import Redis

from .spec import SpiderSpec
from .urls import canonicalize, fingerprint

RUNNING, FINISHED, CANCELLED = "running", "finished", "cancelled"
COUNTERS = ("pending", "enqueued", "fetched", "items", "errors", "retries", "dropped", "bytes")


@dataclass(frozen=True)
class Request:
    job_id: str
    url: str
    page: str
    depth: int = 0
    attempt: int = 0

    def dumps(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def loads(cls, raw: str | bytes) -> Request:
        return cls(**json.loads(raw))


class Frontier:
    def __init__(self, redis: Redis, prefix: str = "cn", inflight_timeout: float = 120.0):
        self.r = redis
        self.p = prefix
        self.inflight_timeout = inflight_timeout
        self._specs: dict[str, SpiderSpec] = {}

    # --- ключи -------------------------------------------------------------
    def k(self, *parts: str) -> str:
        return ":".join((self.p, *parts))

    # --- задачи ------------------------------------------------------------
    async def create_job(self, spec: SpiderSpec) -> str:
        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        meta = {"id": job_id, "spider": spec.name, "status": RUNNING, "created": now, **dict.fromkeys(COUNTERS, 0)}
        async with self.r.pipeline(transaction=True) as pipe:
            pipe.set(self.k("spec", job_id), spec.model_dump_json())
            pipe.hset(self.k("job", job_id), mapping=meta)
            pipe.zadd(self.k("jobs"), {job_id: now})
            await pipe.execute()
        for s in spec.start:
            await self.enqueue(job_id, canonicalize(s.url) or s.url, s.page, depth=0)
        if not await self._int(job_id, "pending"):
            await self._finish(job_id)
        return job_id

    async def spec(self, job_id: str) -> SpiderSpec | None:
        if job_id not in self._specs:
            raw = await self.r.get(self.k("spec", job_id))
            if raw is None:
                return None
            self._specs[job_id] = SpiderSpec.model_validate_json(raw)
        return self._specs[job_id]

    async def job(self, job_id: str) -> dict | None:
        raw = await self.r.hgetall(self.k("job", job_id))
        if not raw:
            return None
        d = {_s(k): _s(v) for k, v in raw.items()}
        for c in COUNTERS:
            d[c] = int(d.get(c, 0))
        for c in ("created", "finished"):
            if c in d:
                d[c] = float(d[c])
        return d

    async def jobs(self, limit: int = 50) -> list[dict]:
        ids = await self.r.zrevrange(self.k("jobs"), 0, limit - 1)
        return [j for j in [await self.job(_s(i)) for i in ids] if j]

    async def status(self, job_id: str) -> str | None:
        v = await self.r.hget(self.k("job", job_id), "status")
        return _s(v) if v is not None else None

    async def cancel(self, job_id: str) -> bool:
        if await self.status(job_id) != RUNNING:
            return False
        await self.r.hset(self.k("job", job_id), mapping={"status": CANCELLED, "finished": time.time()})
        return True

    async def incr(self, job_id: str, field: str, by: int = 1) -> int:
        return await self.r.hincrby(self.k("job", job_id), field, by)

    async def mark_fetched(self, job_id: str, nbytes: int) -> None:
        minute = int(time.time() // 60) * 60
        async with self.r.pipeline(transaction=False) as pipe:
            pipe.hincrby(self.k("job", job_id), "fetched", 1)
            pipe.hincrby(self.k("job", job_id), "bytes", nbytes)
            pipe.hincrby(self.k("rate", job_id), str(minute), 1)
            await pipe.execute()

    async def rate(self, job_id: str) -> list[tuple[int, int]]:
        raw = await self.r.hgetall(self.k("rate", job_id))
        return sorted((int(_s(k)), int(_s(v))) for k, v in raw.items())

    async def dead(self, job_id: str, limit: int = 100) -> list[dict]:
        return [json.loads(x) for x in await self.r.lrange(self.k("dead", job_id), 0, limit - 1)]

    async def _int(self, job_id: str, field: str) -> int:
        return int(await self.r.hget(self.k("job", job_id), field) or 0)

    async def _finish(self, job_id: str) -> None:
        # завершаем только «живую» задачу; отменённая сохраняет свой статус
        if await self.status(job_id) == RUNNING:
            await self.r.hset(self.k("job", job_id), mapping={"status": FINISHED, "finished": time.time()})

    # --- очередь -----------------------------------------------------------
    async def enqueue(self, job_id: str, url: str, page: str, depth: int, delay: float = 0.0) -> bool:
        spec = await self.spec(job_id)
        if spec is None or await self.status(job_id) != RUNNING:
            return False
        if not await self.r.sadd(self.k("seen", job_id), fingerprint(url)):
            return False
        if await self.incr(job_id, "enqueued") > spec.settings.max_pages:
            await self.incr(job_id, "enqueued", -1)
            await self.incr(job_id, "dropped")
            return False
        req = Request(job_id, url, page, depth)
        async with self.r.pipeline(transaction=True) as pipe:
            pipe.hincrby(self.k("job", job_id), "pending", 1)
            pipe.zadd(self.k("frontier"), {req.dumps(): time.time() + delay})
            await pipe.execute()
        return True

    async def pop(self) -> Request | None:
        """Атомарно берёт самый ранний созревший запрос и помечает его «в работе»."""
        got = await self.r.zpopmin(self.k("frontier"), 1)
        if not got:
            return None
        member, score = got[0]
        now = time.time()
        if score > now:
            # самый ранний запрос ещё не созрел — возвращаем на место
            await self.r.zadd(self.k("frontier"), {member: score})
            return None
        await self.r.zadd(self.k("inflight"), {member: now + self.inflight_timeout})
        return Request.loads(member)

    async def next_ready_in(self) -> float | None:
        top = await self.r.zrange(self.k("frontier"), 0, 0, withscores=True)
        return max(0.0, top[0][1] - time.time()) if top else None

    async def defer(self, req: Request, until: float) -> None:
        m = req.dumps()
        async with self.r.pipeline(transaction=True) as pipe:
            pipe.zrem(self.k("inflight"), m)
            pipe.zadd(self.k("frontier"), {m: until})
            await pipe.execute()

    async def retry(self, req: Request, delay: float) -> None:
        nxt = replace(req, attempt=req.attempt + 1)
        async with self.r.pipeline(transaction=True) as pipe:
            pipe.zrem(self.k("inflight"), req.dumps())
            pipe.zadd(self.k("frontier"), {nxt.dumps(): time.time() + delay})
            pipe.hincrby(self.k("job", req.job_id), "retries", 1)
            await pipe.execute()

    async def ack(self, req: Request) -> None:
        async with self.r.pipeline(transaction=True) as pipe:
            pipe.zrem(self.k("inflight"), req.dumps())
            pipe.hincrby(self.k("job", req.job_id), "pending", -1)
            removed, pending = await pipe.execute()
        if not removed:
            # запрос уже вернул reaper и, возможно, обработал другой воркер — не считаем дважды
            await self.incr(req.job_id, "pending", 1)
            return
        if pending <= 0:
            await self._finish(req.job_id)

    async def fail(self, req: Request, reason: str) -> None:
        entry = json.dumps({"url": req.url, "page": req.page, "attempt": req.attempt, "reason": reason, "ts": time.time()})
        async with self.r.pipeline(transaction=False) as pipe:
            pipe.rpush(self.k("dead", req.job_id), entry)
            pipe.hincrby(self.k("job", req.job_id), "errors", 1)
            await pipe.execute()
        await self.ack(req)

    async def drop(self, req: Request) -> None:
        await self.incr(req.job_id, "dropped")
        await self.ack(req)

    async def reap(self) -> int:
        """Возвращает в очередь запросы, чей воркер умер, не успев их подтвердить."""
        now = time.time()
        expired = await self.r.zrangebyscore(self.k("inflight"), "-inf", now, start=0, num=500)
        moved = 0
        for m in expired:
            if await self.r.zrem(self.k("inflight"), m):
                await self.r.zadd(self.k("frontier"), {m: now})
                moved += 1
        return moved

    async def acquire_slot(self, host: str, delay: float) -> float:
        """Вежливость: 0 — можно идти; иначе сколько секунд ждать."""
        if delay <= 0:
            return 0.0
        if await self.r.set(self.k("slot", host), "1", nx=True, px=int(delay * 1000)):
            return 0.0
        ttl = await self.r.pttl(self.k("slot", host))
        return max(ttl, 50) / 1000

    async def is_idle(self) -> bool:
        return not await self.r.zcard(self.k("frontier")) and not await self.r.zcard(self.k("inflight"))

    # --- воркеры -----------------------------------------------------------
    async def heartbeat(self, worker_id: str, info: dict) -> None:
        await self.r.hset(self.k("workers"), worker_id, json.dumps({**info, "ts": time.time()}))

    async def workers(self, alive_within: float = 15.0) -> list[dict]:
        raw = await self.r.hgetall(self.k("workers"))
        now, out = time.time(), []
        for wid, v in raw.items():
            d = json.loads(v)
            if now - d["ts"] > 3600:
                await self.r.hdel(self.k("workers"), wid)
                continue
            out.append({"id": _s(wid), **d, "alive": now - d["ts"] <= alive_within})
        return sorted(out, key=lambda w: w["id"])

    async def queue_sizes(self) -> dict[str, int]:
        return {"frontier": await self.r.zcard(self.k("frontier")), "inflight": await self.r.zcard(self.k("inflight"))}


def _s(v: bytes | str) -> str:
    return v.decode() if isinstance(v, bytes) else v
