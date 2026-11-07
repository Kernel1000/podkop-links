"""Экраны «Серверы» и «Добавить сервер»."""
import ipaddress
import re
import tempfile
from html import escape


def e(s) -> str:
    return escape(str(s), quote=False)

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup, Message

import links
import ssh
import storage
from storage import Server

router = Router()
BTN_SERVERS = "🖥 Серверы"
BTN_ADD = "➕ Добавить сервер"
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


class Add(StatesGroup):
    host = State()
    password = State()
    name = State()
    slot = State()


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


CANCEL = B(text="Отмена", callback_data="add:cancel")


def slot_label(slot: int | None) -> str:
    return "резерв" if slot is None else f"слот {slot}"


def parse_host(text: str) -> tuple[str, int] | None:
    t = text.strip()
    host, port = t, 22
    if t.count(":") == 1:
        host, p = t.split(":")
        if not p.isdigit():
            return None
        port = int(p)
    host = host.strip("[]")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not HOST_RE.match(host):
            return None
    return (host, port) if 0 < port < 65536 else None


# ---------- список и карточка ----------

def servers_list() -> tuple[str, InlineKeyboardMarkup | None]:
    ss = storage.load()
    if not ss:
        return "Серверов пока нет. Нажми «➕ Добавить сервер».", None
    rows = [[B(text=f"{s.name} · {slot_label(s.slot)}", callback_data=f"srv:{s.name}")] for s in ss]
    return "<b>Серверы</b>", InlineKeyboardMarkup(inline_keyboard=rows)


async def card(name: str) -> tuple[str, InlineKeyboardMarkup]:
    back = B(text="« К списку", callback_data="list")
    s = storage.get(name)
    if not s:
        return "Сервер не найден.", kb([back])
    lines = [f"🖥 <b>{e(s.name)}</b> · <code>{e(s.addr)}</code>", f"Слот: {slot_label(s.slot)}"]
    try:
        async with ssh.connect(s) as conn:
            inf = await ssh.info(conn)
            with tempfile.TemporaryDirectory() as tmp:
                db = await ssh.fetch_xui_db(conn, tmp)
                routers = links.parse_xui_db(db, s.host, s.slot or 0)[0] if db else None
        lines.append(f"SSH: 🟢 · {e(inf.get('os', '?'))}")
        if inf.get("xui") == "1":
            act = inf.get("xui_active")
            lines.append(f"3x-ui: {'🟢' if act == 'active' else '🔴 ' + e(act or '?')} · Xray {e(inf.get('xray', '?'))}")
        else:
            lines.append("3x-ui: не установлен")
        if routers is not None:
            names = ", ".join(sorted(routers)) or "—"
            lines.append(f"Роутеров с меткой «{e(links.ROUTER_TAG)}»: {len(routers)}\n<i>{e(names)}</i>")
    except ssh.SSHError as err:
        lines.append(f"SSH: 🔴 {e(str(err))}")
    markup = kb([B(text="🔄 Обновить", callback_data=f"srv:{name}"),
                 B(text="🔢 Слот", callback_data=f"slotmenu:{name}")],
                [B(text="🗑 Удалить", callback_data=f"del:{name}"), back])
    return "\n".join(lines), markup


@router.message(Command("servers"))
@router.message(F.text == BTN_SERVERS)
async def show_list(m: Message):
    text, markup = servers_list()
    await m.answer(text, reply_markup=markup)


@router.callback_query(F.data == "list")
async def cb_list(c: CallbackQuery):
    text, markup = servers_list()
    await c.message.edit_text(text, reply_markup=markup)
    await c.answer()


@router.callback_query(F.data.startswith("srv:"))
async def cb_card(c: CallbackQuery):
    await c.answer()
    await c.message.edit_text("⏳ Проверяю сервер…")
    text, markup = await card(c.data.split(":", 1)[1])
    await c.message.edit_text(text, reply_markup=markup)


# ---------- выбор слота ----------

def slot_buttons(prefix: str, me: str | None) -> list[list[B]]:
    rows = []
    for sl in storage.slots():
        owner = storage.in_slot(sl)
        mark = "" if owner is None or owner.name == me else f" (сейчас {owner.name})"
        rows.append([B(text=f"Слот {sl}{mark}", callback_data=f"{prefix}{sl}")])
    nxt = (max(storage.slots()) + 1) if storage.slots() else 1
    rows.append([B(text=f"➕ Новый слот {nxt}", callback_data=f"{prefix}{nxt}"),
                 B(text="Резерв", callback_data=f"{prefix}r")])
    return rows


def _slot(v: str) -> int | None:
    return None if v == "r" else int(v)


@router.callback_query(F.data.startswith("slotmenu:"))
async def cb_slotmenu(c: CallbackQuery):
    name = c.data.split(":", 1)[1]
    rows = slot_buttons(f"setslot:{name}:", name) + [[B(text="« Назад", callback_data=f"srv:{name}")]]
    await c.message.edit_text(f"Какой слот у <b>{e(name)}</b>?", reply_markup=kb(*rows))
    await c.answer()


@router.callback_query(F.data.startswith("setslot:") | F.data.startswith("setslotok:"))
async def cb_setslot(c: CallbackQuery):
    action, name, v = c.data.split(":")
    s, slot = storage.get(name), _slot(v)
    if not s:
        return await c.answer("Сервер не найден", show_alert=True)
    owner = storage.in_slot(slot) if slot is not None else None
    if owner and owner.name != name and action == "setslot":
        await c.message.edit_text(
            f"Слот {slot} сейчас у <b>{e(owner.name)}</b>. Он уйдёт в резерв, его ссылки при следующей выгрузке "
            f"заменятся ссылками <b>{e(name)}</b>. Продолжить?",
            reply_markup=kb([B(text="Да", callback_data=f"setslotok:{name}:{v}"),
                             B(text="Нет", callback_data=f"srv:{name}")]))
        return await c.answer()
    s.slot = slot
    storage.upsert(s)
    await c.answer(f"Теперь {slot_label(slot)}")
    await c.message.edit_text("⏳ Проверяю сервер…")
    text, markup = await card(name)
    await c.message.edit_text(text + "\n\n💡 Чтобы роутеры получили изменения — «🚀 Выгрузить ссылки».",
                              reply_markup=markup)


# ---------- удаление ----------

@router.callback_query(F.data.startswith("del:"))
async def cb_del(c: CallbackQuery):
    name = c.data.split(":", 1)[1]
    s = storage.get(name)
    warn = (f"\nСсылки слота {s.slot} при следующей выгрузке пропадут с роутеров."
            if s and s.slot is not None else "")
    await c.message.edit_text(f"Удалить <b>{e(name)}</b> из списка? Ключ бота на сервере останется.{warn}",
                              reply_markup=kb([B(text="🗑 Удалить", callback_data=f"delok:{name}"),
                                               B(text="Нет", callback_data=f"srv:{name}")]))
    await c.answer()


@router.callback_query(F.data.startswith("delok:"))
async def cb_delok(c: CallbackQuery):
    storage.remove(c.data.split(":", 1)[1])
    await c.answer("Удалён")
    text, markup = servers_list()
    await c.message.edit_text(text, reply_markup=markup)


# ---------- добавление ----------

@router.message(Command("add"))
@router.message(F.text == BTN_ADD)
async def add_start(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Add.host)
    await m.answer("Введи IP сервера (если SSH не на 22 порту — <code>IP:порт</code>).", reply_markup=kb([CANCEL]))


@router.callback_query(F.data == "add:cancel")
async def add_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("Отменено.")
    await c.answer()


@router.message(Add.host)
async def add_host(m: Message, state: FSMContext):
    hp = parse_host(m.text or "")
    if not hp:
        return await m.answer("Не похоже на адрес. Пример: <code>181.214.99.111</code> или <code>1.2.3.4:2222</code>",
                              reply_markup=kb([CANCEL]))
    old = storage.by_host(*hp)
    await state.update_data(host=hp[0], port=hp[1], old=old.name if old else None)
    await state.set_state(Add.password)
    note = f"\nЭтот адрес уже есть как <b>{e(old.name)}</b> — ключ переустановлю, данные обновлю." if old else ""
    await m.answer(f"Пароль root для <code>{e(hp[0])}</code> (сообщение с паролем сразу удалю).{note}",
                   reply_markup=kb([CANCEL]))


@router.message(Add.password)
async def add_password(m: Message, state: FSMContext):
    password = m.text or ""
    try:
        await m.delete()
    except Exception:  # noqa: BLE001
        pass
    d = await state.get_data()
    status = await m.answer("⏳ Подключаюсь и устанавливаю ключ…")
    try:
        await ssh.install_key(d["host"], d["port"], password)
        async with ssh.connect(Server("new", d["host"], d["port"])) as conn:
            inf = await ssh.info(conn)
    except Exception as err:  # noqa: BLE001
        return await status.edit_text(f"❌ {e(str(err))}\nПришли пароль ещё раз или нажми «Отмена».",
                                      reply_markup=kb([CANCEL]))
    finally:
        del password
    suggested = d["old"] or re.sub(r"[^A-Za-z0-9._-]", "-", inf.get("hostname", ""))[:32] or "server"
    await state.update_data(suggested=suggested)
    await state.set_state(Add.name)
    xui = f"🟢 Xray {e(inf.get('xray', '?'))}" if inf.get("xui") == "1" else "не установлен"
    await status.edit_text(
        f"✅ Ключ бота установлен, дальше вход без пароля.\n"
        f"Хост: <code>{e(inf.get('hostname', '?'))}</code>\nОС: {e(inf.get('os', '?'))}\n3x-ui: {xui}\n\n"
        "Как назвать сервер? Напиши имя (латиница, цифры, <code>.-_</code>) или нажми кнопку.",
        reply_markup=kb([B(text=f"Оставить «{suggested}»", callback_data="add:name")], [CANCEL]))


async def _ask_slot(m: Message, state: FSMContext, name: str, edit: bool):
    await state.update_data(name=name)
    await state.set_state(Add.slot)
    rows = slot_buttons("add:slot:", None) + [[CANCEL]]
    text = f"Сервер <b>{e(name)}</b>. Какой слот?"
    await (m.edit_text(text, reply_markup=kb(*rows)) if edit else m.answer(text, reply_markup=kb(*rows)))


def _name_error(name: str, d: dict) -> str | None:
    if not NAME_RE.match(name):
        return "Имя: латиница, цифры, точка, дефис, подчёркивание, до 32 символов."
    other = storage.get(name)
    if other and (other.host, other.port) != (d["host"], d["port"]):
        return f"Имя «{e(name)}» уже занято сервером {e(other.addr)}."
    return None


@router.message(Add.name)
async def add_name(m: Message, state: FSMContext):
    name, d = (m.text or "").strip(), await state.get_data()
    if err := _name_error(name, d):
        return await m.answer(err, reply_markup=kb([CANCEL]))
    await _ask_slot(m, state, name, edit=False)


@router.callback_query(Add.name, F.data == "add:name")
async def add_name_btn(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if err := _name_error(d["suggested"], d):
        await c.message.answer(err + " Напиши другое имя.", reply_markup=kb([CANCEL]))
        return await c.answer()
    await _ask_slot(c.message, state, d["suggested"], edit=True)
    await c.answer()


@router.message(Add.slot)
async def add_slot_text(m: Message):
    await m.answer("Выбери слот кнопкой в сообщении выше (или /cancel).")


@router.callback_query(Add.slot, F.data.startswith("add:slot:") | F.data.startswith("add:slotok:"))
async def add_slot(c: CallbackQuery, state: FSMContext):
    action, v = c.data.split(":")[1:]
    d, slot = await state.get_data(), _slot(v)
    owner = storage.in_slot(slot) if slot is not None else None
    if owner and owner.name not in (d["name"], d["old"]) and action == "slot":
        await c.message.edit_text(
            f"Слот {slot} сейчас у <b>{e(owner.name)}</b>. Он уйдёт в резерв, а ссылки слота {slot} при следующей "
            f"выгрузке заменятся ссылками <b>{e(d['name'])}</b>. Продолжить?",
            reply_markup=kb([B(text="Да", callback_data=f"add:slotok:{v}")], [CANCEL]))
        return await c.answer()
    storage.upsert(Server(d["name"], d["host"], d["port"], slot), replace_name=d["old"])
    await state.clear()
    await c.answer("Сервер добавлен")
    await c.message.edit_text("⏳ Проверяю сервер…")
    text, markup = await card(d["name"])
    tip = "\n\n💡 Чтобы роутеры получили его ссылки — «🚀 Выгрузить ссылки»." if slot is not None else ""
    await c.message.edit_text(text + tip, reply_markup=markup)
