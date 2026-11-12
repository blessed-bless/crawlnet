from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from pathlib import Path

import typer

from .config import settings
from .runtime import open_runtime
from .spec import SpiderSpec
from .worker import Worker

app = typer.Typer(help="crawlnet — распределённый краулер", no_args_is_help=True, add_completion=False)


def _logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _fmt_job(j: dict) -> str:
    took = (j.get("finished") or time.time()) - j["created"]
    return (
        f"{j['id']}  {j['spider']:<12} {j['status']:<9} "
        f"страниц {j['fetched']:>5}  записей {j['items']:>5}  в очереди {j['pending']:>4}  "
        f"ошибок {j['errors']:>3}  ретраев {j['retries']:>3}  {took:6.1f}с"
    )


@app.command()
def validate(spider: Path) -> None:
    """Проверить YAML-спайдер."""
    spec = SpiderSpec.from_file(spider)
    typer.echo(f"OK: {spec.name}, типов страниц: {len(spec.pages)}, стартовых URL: {len(spec.start)}")


@app.command()
def submit(spider: str = typer.Argument(..., help="имя из каталога spiders/ или путь к YAML")) -> None:
    """Поставить задачу в общую очередь (её подхватят воркеры)."""

    async def main():
        async with open_runtime() as rt:
            job_id = await rt.frontier.create_job(rt.load_spider(spider, allow_path=True))
            typer.echo(job_id)

    asyncio.run(main())


@app.command()
def worker(
    concurrency: int = typer.Option(16, "-c", help="параллельных запросов в этом процессе"),
    verbose: bool = typer.Option(False, "-v"),
) -> None:
    """Запустить воркер. Можно поднять сколько угодно на разных машинах."""
    _logging(verbose)

    async def main():
        async with open_runtime() as rt:
            w = Worker(rt.frontier, rt.storage, concurrency)
            loop = asyncio.get_running_loop()
            if sys.platform != "win32":
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(sig, w.stop)
            logging.info("воркер %s запущен, слотов: %d", w.id, concurrency)
            try:
                await w.run()
            finally:
                await w.aclose()

    asyncio.run(main())


@app.command()
def run(
    spider: str,
    concurrency: int = typer.Option(8, "-c"),
    export: str | None = typer.Option(None, "--export", "-e", help="csv или xlsx"),
    local: bool = typer.Option(True, "--local/--redis", help="очередь в памяти или в настоящем Redis"),
    verbose: bool = typer.Option(False, "-v"),
) -> None:
    """Прогнать спайдер целиком в одном процессе и дождаться конца."""
    _logging(verbose)

    async def main():
        async with open_runtime(local=local) as rt:
            job_id = await rt.frontier.create_job(rt.load_spider(spider, allow_path=True))
            typer.echo(f"задача {job_id}")
            w = Worker(rt.frontier, rt.storage, concurrency)

            async def progress():
                while True:
                    await asyncio.sleep(2)
                    typer.echo(_fmt_job(await rt.frontier.job(job_id)))

            p = asyncio.create_task(progress())
            try:
                await w.run(until_idle=True)
            finally:
                p.cancel()
                await w.aclose()
            typer.echo(_fmt_job(await rt.frontier.job(job_id)))
            if export:
                data, _, name = await rt.export(job_id, export)
                Path(name).write_bytes(data)
                typer.echo(f"сохранено: {name}")

    asyncio.run(main())


@app.command()
def jobs(limit: int = 20) -> None:
    """Список задач."""

    async def main():
        async with open_runtime() as rt:
            for j in await rt.frontier.jobs(limit):
                typer.echo(_fmt_job(j))

    asyncio.run(main())


@app.command()
def cancel(job_id: str) -> None:
    """Отменить задачу: воркеры выбросят её оставшиеся запросы."""

    async def main():
        async with open_runtime() as rt:
            typer.echo("отменено" if await rt.frontier.cancel(job_id) else "задача не выполняется")

    asyncio.run(main())


@app.command()
def export(job_id: str, fmt: str = typer.Option("xlsx", "--format", "-f"), out: Path | None = typer.Option(None, "-o")) -> None:
    """Выгрузить результаты задачи в CSV или Excel."""

    async def main():
        async with open_runtime() as rt:
            data, _, name = await rt.export(job_id, fmt)
            path = out or Path(name)
            path.write_bytes(data)
            typer.echo(f"сохранено: {path}")

    asyncio.run(main())


@app.command()
def dashboard(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Веб-дашборд с живой статистикой."""
    import uvicorn

    uvicorn.run("crawlnet.dashboard.app:app", host=host, port=port)


@app.command()
def bot() -> None:
    """Telegram-бот (нужен CRAWLNET_TG_TOKEN)."""
    _logging(False)
    from .bot import run_bot

    if not settings.tg_token:
        raise typer.BadParameter("задайте CRAWLNET_TG_TOKEN")
    asyncio.run(run_bot())


if __name__ == "__main__":
    app()
