"""Telegram-бот: запуск спайдеров, статус, выгрузка файлов, уведомление о завершении."""
from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import settings
from .frontier import RUNNING
from .runtime import Runtime, open_runtime

log = logging.getLogger("crawlnet.bot")


class AllowList(BaseMiddleware):
    """Пускает только пользователей из CRAWLNET_TG_ALLOWED; пустой список — не пускает никого."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or user.id not in settings.tg_allowed:
            return None
        return await handler(event, data)


def job_text(j: dict) -> str:
    return (
        f"<b>{j['spider']}</b> · <code>{j['id']}</code> · {j['status']}\n"
        f"страниц {j['fetched']}, записей {j['items']}, в очереди {j['pending']}, "
        f"ошибок {j['errors']}, ретраев {j['retries']}"
    )


def job_kb(job_id: str, running: bool) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(text="CSV", callback_data=f"exp:{job_id}:csv"),
        InlineKeyboardButton(text="Excel", callback_data=f"exp:{job_id}:xlsx"),
    ]
    if running:
        row.append(InlineKeyboardButton(text="Отменить", callback_data=f"cancel:{job_id}"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def build_dispatcher(rt: Runtime) -> Dispatcher:
    dp = Dispatcher()
    dp.message.outer_middleware(AllowList())
    dp.callback_query.outer_middleware(AllowList())
    watchers: set[asyncio.Task] = set()

    async def watch(bot: Bot, chat_id: int, job_id: str) -> None:
        while (j := await rt.frontier.job(job_id)) and j["status"] == RUNNING:
            await asyncio.sleep(5)
        if j:
            await bot.send_message(chat_id, "Готово ✅\n" + job_text(j), reply_markup=job_kb(job_id, False))

    @dp.message(Command("start", "help"))
    async def help_(m: Message):
        await m.answer(
            "Команды:\n/spiders — доступные спайдеры\n/run &lt;имя&gt; — запустить\n"
            "/jobs — последние задачи\n/status &lt;id&gt; — состояние задачи"
        )

    @dp.message(Command("spiders"))
    async def spiders(m: Message):
        names = sorted(rt.spider_files())
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"▶ {n}", callback_data=f"run:{n}")] for n in names])
        await m.answer("Спайдеры:" if names else "Каталог спайдеров пуст", reply_markup=kb)

    async def launch(bot: Bot, chat_id: int, name: str) -> str:
        job_id = await rt.frontier.create_job(rt.load_spider(name))
        t = asyncio.create_task(watch(bot, chat_id, job_id))
        watchers.add(t)
        t.add_done_callback(watchers.discard)
        return job_id

    @dp.message(Command("run"))
    async def run(m: Message, command: CommandObject, bot: Bot):
        if not command.args:
            await m.answer("Укажите имя: /run books")
            return
        try:
            job_id = await launch(bot, m.chat.id, command.args.strip())
        except KeyError as e:
            await m.answer(str(e))
            return
        await m.answer(f"Запущено: <code>{job_id}</code>. Напишу, когда закончится.")

    @dp.callback_query(F.data.startswith("run:"))
    async def run_cb(c: CallbackQuery, bot: Bot):
        job_id = await launch(bot, c.message.chat.id, c.data.split(":", 1)[1])
        await c.message.answer(f"Запущено: <code>{job_id}</code>. Напишу, когда закончится.")
        await c.answer()

    @dp.message(Command("jobs"))
    async def jobs(m: Message):
        js = await rt.frontier.jobs(10)
        await m.answer("\n\n".join(map(job_text, js)) or "Задач пока нет")

    @dp.message(Command("status"))
    async def status(m: Message, command: CommandObject):
        j = await rt.frontier.job((command.args or "").strip())
        if not j:
            await m.answer("Нет такой задачи")
            return
        await m.answer(job_text(j), reply_markup=job_kb(j["id"], j["status"] == RUNNING))

    @dp.callback_query(F.data.startswith("exp:"))
    async def export(c: CallbackQuery):
        _, job_id, fmt = c.data.split(":")
        await c.answer("Готовлю файл…")
        data, _, name = await rt.export(job_id, fmt)
        await c.message.answer_document(BufferedInputFile(data, filename=name))

    @dp.callback_query(F.data.startswith("cancel:"))
    async def cancel(c: CallbackQuery):
        ok = await rt.frontier.cancel(c.data.split(":", 1)[1])
        await c.answer("Отменено" if ok else "Задача уже не выполняется")

    return dp


async def run_bot() -> None:
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    if not settings.tg_allowed:
        raise RuntimeError("задайте CRAWLNET_TG_ALLOWED — id пользователей, которым разрешён бот")
    bot = Bot(settings.tg_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    async with open_runtime() as rt:
        dp = build_dispatcher(rt)
        log.info("бот запущен")
        with contextlib.suppress(KeyboardInterrupt):
            await dp.start_polling(bot)
