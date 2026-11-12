from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from ..runtime import Runtime, open_runtime
from ..storage import EXPORTERS

STATIC = Path(__file__).parent / "static"


class Submit(BaseModel):
    spider: str


def basic_auth(password: str):
    """HTTP Basic: логин любой, пароль сравнивается за постоянное время."""
    scheme = HTTPBasic(realm="crawlnet")

    def check(creds: HTTPBasicCredentials = Depends(scheme)) -> None:
        if not secrets.compare_digest(creds.password.encode(), password.encode()):
            raise HTTPException(401, "неверный пароль", headers={"WWW-Authenticate": 'Basic realm="crawlnet"'})

    return check


def create_app(runtime_factory=open_runtime, password: str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with runtime_factory() as rt:
            app.state.rt = rt
            yield

    deps = [Depends(basic_auth(password))] if password else []
    app = FastAPI(title="crawlnet", lifespan=lifespan, dependencies=deps)

    def rt(request: Request) -> Runtime:
        return request.app.state.rt

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/overview")
    async def overview(r: Runtime = Depends(rt)):
        return {
            "queue": await r.frontier.queue_sizes(),
            "workers": await r.frontier.workers(),
            "jobs": await r.frontier.jobs(30),
            "spiders": sorted(r.spider_files()),
        }

    @app.get("/api/jobs/{job_id}")
    async def job(job_id: str, r: Runtime = Depends(rt)):
        j = await r.frontier.job(job_id)
        if j is None:
            raise HTTPException(404, "нет такой задачи")
        return {
            **j,
            "rate": await r.frontier.rate(job_id),
            "dead": await r.frontier.dead(job_id, 50),
            "sample": [
                {"url": x["url"], **x["data"]} for x in await r.storage.fetch(job_id=job_id, limit=20)
            ],
        }

    @app.post("/api/jobs", status_code=201)
    async def submit(body: Submit, r: Runtime = Depends(rt)):
        try:
            spec = r.load_spider(body.spider)
        except KeyError as e:
            raise HTTPException(404, str(e)) from None
        return {"id": await r.frontier.create_job(spec)}

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel(job_id: str, r: Runtime = Depends(rt)):
        return {"cancelled": await r.frontier.cancel(job_id)}

    @app.get("/api/jobs/{job_id}/export")
    async def export(job_id: str, format: str = "xlsx", r: Runtime = Depends(rt)):
        if format not in EXPORTERS:
            raise HTTPException(400, "format: csv или xlsx")
        try:
            data, mime, name = await r.export(job_id, format)
        except KeyError:
            raise HTTPException(404, "нет такой задачи") from None
        return Response(data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    return app


app = create_app(password=os.environ.get("CRAWLNET_DASHBOARD_PASSWORD") or None)
