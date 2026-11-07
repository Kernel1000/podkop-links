"""podkop-bot — управляющий Telegram-бот: серверы 3x-ui и раздача vless-ссылок роутерам через GitHub."""
import asyncio
import logging
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from html import escape


def e(s) -> str:
    return escape(str(s), quote=False)

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)

import config
import links
import servers
import ssh
from github import GitHub

VERSION = "1.0.0"
BTN_PUSH = "🚀 Выгрузить ссылки"
BTN_STATUS = "📊 Статус ссылок"
MENU = ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True, keyboard=[
    [KeyboardButton(text=servers.BTN_SERVERS), KeyboardButton(text=servers.BTN_ADD)],
    [KeyboardButton(text=BTN_PUSH), KeyboardButton(text=BTN_STATUS)],
])

log = logging.getLogger("podkop-bot")
dp = Dispatcher()
gh = GitHub()
PENDING: dict[str, tuple[links.Plan, str, float]] = {}  # подготовленные выгрузки до подтверждения
PUSH_LOCK = asyncio.Lock()


# ---------- доступ только для администраторов ----------

async def admin_only(handler, event, data):
    user = getattr(event, "from_user", None)
    if user and user.id in config.ADMIN_IDS:
        return await handler(event, data)
    log.warning("чужой пользователь %s (%s)", user and user.id, user and user.username)
    if isinstance(event, Message):
        await event.answer(f"Нет доступа. Твой Telegram ID: <code>{user.id}</code>")
    elif isinstance(event, CallbackQuery):
        await event.answer("Нет доступа", show_alert=True)


dp.message.outer_middleware(admin_only)
dp.callback_query.outer_middleware(admin_only)


# ---------- общее ----------

@dp.message(CommandStart())
@dp.message(Command("help"))
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(
        f"<b>podkop-bot {VERSION}</b>\n\n"
        f"{servers.BTN_SERVERS} — список, состояние, слоты\n"
        f"{servers.BTN_ADD} — по IP и паролю root\n"
        f"{BTN_PUSH} — собрать ссылки с серверов и выложить в GitHub\n"
        f"{BTN_STATUS} — актуально ли то, что в GitHub\n\n"
        "/cancel — прервать текущее действие", reply_markup=MENU)


@dp.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.", reply_markup=MENU)


# ---------- ссылки ----------

async def build() -> tuple[links.Collected, links.Plan]:
    col, (old, legacy) = await asyncio.gather(links.collect(), gh.read_state())
    return col, links.make_plan(col, old, legacy)


def ago(iso: str) -> str:
    sec = (datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds()
    if sec < 3600:
        return f"{int(sec // 60)} мин назад"
    if sec < 86400:
        return f"{int(sec // 3600)} ч назад"
    return f"{int(sec // 86400)} дн назад"


@dp.message(Command("status"))
@dp.message(F.text == BTN_STATUS)
async def status(m: Message):
    msg = await m.answer("⏳ Сверяю серверы с GitHub…")
    try:
        (col, plan), last = await asyncio.gather(build(), gh.last_commit())
    except Exception as err:  # noqa: BLE001
        log.exception("status")
        return await msg.edit_text(f"❌ {e(str(err))}")
    text = links.plan_text(col, plan)
    if not plan.empty and col.per_slot:
        text += f"\n\nНужна выгрузка — «{BTN_PUSH}»."
    if last:
        text += f"\n\nПоследняя выгрузка: {ago(last[0])} · <i>{e(last[1])}</i>"
    text += f"\nРоутеров в GitHub после выгрузки: {plan.total}"
    await msg.edit_text(text)


@dp.message(Command("push"))
@dp.message(F.text == BTN_PUSH)
async def push(m: Message):
    msg = await m.answer("⏳ Собираю ссылки с серверов…")
    try:
        col, plan = await build()
    except Exception as err:  # noqa: BLE001
        log.exception("push")
        return await msg.edit_text(f"❌ {e(str(err))}")
    text = links.plan_text(col, plan)
    if plan.empty or not col.per_slot:
        return await msg.edit_text(text)
    token = secrets.token_hex(4)
    PENDING[token] = (plan, text, time.time())
    await msg.edit_text(text + "\n\nВыгрузить в GitHub?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        B(text="✅ Выгрузить", callback_data=f"push:{token}"), B(text="Отмена", callback_data=f"pushno:{token}")]]))


@dp.callback_query(F.data.startswith("push:"))
async def push_ok(c: CallbackQuery):
    item = PENDING.pop(c.data.split(":", 1)[1], None)
    if not item or time.time() - item[2] > 900:
        await c.answer("Устарело, нажми «Выгрузить ссылки» ещё раз", show_alert=True)
        return await c.message.edit_reply_markup(reply_markup=None)
    plan, text, _ = item
    await c.answer()
    await c.message.edit_text(text + "\n\n⏳ Выгружаю…")
    msg = "podkop-bot: " + " ".join(p for p in (
        f"+{len(plan.added)}" if plan.added else "", f"~{len(plan.changed)}" if plan.changed else "",
        f"-{len(plan.removed)}" if plan.removed else "", "cleanup" if plan.legacy else "") if p)
    try:
        async with PUSH_LOCK:
            sha = await gh.commit(plan.files, msg)
    except Exception as err:  # noqa: BLE001
        log.exception("commit")
        return await c.message.edit_text(text + f"\n\n❌ {e(str(err))}")
    await c.message.edit_text(text + f"\n\n🚀 Выгружено (коммит <code>{sha[:7]}</code>). "
                                     "Роутеры подхватят в течение ~10 минут.")


@dp.callback_query(F.data.startswith("pushno:"))
async def push_no(c: CallbackQuery):
    PENDING.pop(c.data.split(":", 1)[1], None)
    await c.message.edit_reply_markup(reply_markup=None)
    await c.answer("Отменено")


# ---------- запуск ----------

async def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    missing = [k for k in ("TG_TOKEN", "ADMIN_IDS", "GH_TOKEN", "GH_REPO") if not os.environ.get(k)]
    if missing:
        sys.exit(f"Не заданы {', '.join(missing)} в {config.CONF_DIR}/config.env")
    os.makedirs(config.CONF_DIR, exist_ok=True)
    ssh.ensure_key()
    open(config.KNOWN_HOSTS, "a").close()

    bot = Bot(config.TG_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp.include_router(servers.router)
    await bot.set_my_commands([
        BotCommand(command="servers", description="Серверы"),
        BotCommand(command="add", description="Добавить сервер"),
        BotCommand(command="push", description="Выгрузить ссылки"),
        BotCommand(command="status", description="Статус ссылок"),
        BotCommand(command="cancel", description="Прервать действие"),
    ])
    for uid in config.ADMIN_IDS:
        try:
            await bot.send_message(uid, f"🟢 podkop-bot {VERSION} запущен", reply_markup=MENU)
        except Exception as err:  # noqa: BLE001
            log.warning("не смог написать %s: %s", uid, err)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
