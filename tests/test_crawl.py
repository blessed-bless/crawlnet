import asyncio
import io
from contextlib import asynccontextmanager

from conftest import item_html, page
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from crawlnet.config import Settings
from crawlnet.dashboard.app import create_app
from crawlnet.frontier import FINISHED
from crawlnet.runtime import Runtime
from crawlnet.storage import to_csv, to_xlsx
from crawlnet.worker import Worker


async def crawl(frontier, storage, spec, workers=1):
    job = await frontier.create_job(spec)
    ws = [Worker(frontier, storage, concurrency=4, worker_id=f"w{i}") for i in range(workers)]
    await asyncio.wait_for(asyncio.gather(*(w.run(until_idle=True) for w in ws)), 20)
    for w in ws:
        await w.aclose()
    return job


async def test_full_crawl(shop, frontier, storage, spec):
    job = await crawl(frontier, storage, spec)
    j = await frontier.job(job)

    assert j["status"] == FINISHED and j["pending"] == 0
    rows = {r["key"]: r["data"] for r in await storage.fetch(job_id=job)}
    assert rows == {
        "A-1": {"sku": "A-1", "title": "Чайник", "price": 1990, "tags": ["a", "b"]},
        "A-2": {"sku": "A-2", "title": "Кружка", "price": 350, "tags": ["a", "b"]},
        "A-3": {"sku": "A-3", "title": "Ложка", "price": 1990, "tags": ["a", "b"]},
    }
    assert j["retries"] == 1  # /p/3 ответил 503
    assert j["errors"] == 2  # /p/404 и /p/bad без обязательных полей
    assert j["dropped"] == 1  # /private/ закрыт в robots.txt
    assert {c.request.url.host for c in shop.calls} == {"shop.test"}  # внешний домен не трогали
    assert [d["reason"] for d in await frontier.dead(job)] == ["HTTP 404"]
    # /p/2 и /p/2?utm_source=x — один URL после канонизации
    assert sum(1 for c in shop.calls if c.request.url.path == "/p/2") == 1


async def test_several_workers_share_queue(shop, frontier, storage, spec):
    job = await crawl(frontier, storage, spec, workers=3)
    assert (await frontier.job(job))["items"] == 3
    assert len(await frontier.workers()) == 3


async def test_recrawl_counts_versions(shop, frontier, storage, spec):
    await crawl(frontier, storage, spec)
    shop.get("/p/1").mock(return_value=page(item_html("A-1", "Чайник", "1 490 ₽")))  # подешевел
    shop.get("/p/3").mock(return_value=page(item_html("A-3", "Ложка")))
    await crawl(frontier, storage, spec)
    rows = {r["key"]: r for r in await storage.fetch(spider="shop")}
    assert rows["A-1"]["versions"] == 2 and rows["A-1"]["data"]["price"] == 1490
    assert rows["A-2"]["versions"] == 1


async def test_exports(shop, frontier, storage, spec):
    job = await crawl(frontier, storage, spec)
    rows = await storage.fetch(job_id=job)
    csv_text = to_csv(rows).decode("utf-8-sig")
    assert csv_text.splitlines()[0] == "url,sku,title,price,tags,versions,updated_at"
    assert "Чайник" in csv_text and "a; b" in csv_text
    ws = load_workbook(io.BytesIO(to_xlsx(rows))).active
    assert ws.max_row == 4 and ws["C1"].value == "title"


def test_dashboard_api(tmp_path, spec):
    from fakeredis import FakeAsyncRedis

    from crawlnet.frontier import Frontier
    from crawlnet.storage import Storage

    (tmp_path / "shop.yaml").write_text(spec.model_dump_json(), encoding="utf-8")
    cfg = Settings(db_url=f"sqlite+aiosqlite:///{tmp_path / 'd.db'}", spiders_dir=tmp_path, prefix="d")

    @asynccontextmanager
    async def factory():
        r, s = FakeAsyncRedis(), Storage(cfg.db_url)
        await s.init()
        yield Runtime(r, Frontier(r, "d"), s, cfg)
        await s.close()

    with TestClient(create_app(factory)) as c:
        assert c.get("/api/overview").json()["spiders"] == ["shop"]
        job_id = c.post("/api/jobs", json={"spider": "shop"}).json()["id"]
        assert c.get(f"/api/jobs/{job_id}").json()["pending"] == 1
        assert c.post("/api/jobs", json={"spider": "nope"}).status_code == 404
        r = c.get(f"/api/jobs/{job_id}/export?format=csv")
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
        assert c.post(f"/api/jobs/{job_id}/cancel").json() == {"cancelled": True}
        assert "crawl" in c.get("/").text
