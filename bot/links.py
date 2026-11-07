"""Сборка vless-ссылок из баз 3x-ui и план изменений для GitHub (файл на роутер: routers/<имя>.txt)."""
import asyncio
import json
import re
import sqlite3
import tempfile
from dataclasses import dataclass, field
from urllib.parse import quote

import ssh
import storage
from config import ROUTER_TAG

NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")  # имя роутера = Email клиента, оно же имя файла
DIR = "routers"


def _q(s) -> str:
    return quote(str(s), safe="")


def parse_xui_db(path: str, addr: str, slot: int) -> tuple[dict[str, str], list[str]]:
    """{имя роутера: ссылка} для клиентов с меткой ROUTER_TAG + список пропущенных имён."""
    tag_re = re.compile(rf"\b{re.escape(ROUTER_TAG)}\b", re.I)
    host = f"[{addr}]" if ":" in addr else addr
    links, skipped = {}, []
    db = sqlite3.connect(path)
    try:
        rows = db.execute("select port, settings, stream_settings, remark from inbounds "
                          "where protocol='vless' and enable=1").fetchall()
    finally:
        db.close()
    for port, settings, stream, remark in rows:
        s = json.loads(stream or "{}")
        if s.get("security") != "reality" or s.get("network", "tcp") != "tcp":
            continue
        r = s.get("realitySettings") or {}
        rs = r.get("settings") or {}
        for c in (json.loads(settings or "{}").get("clients") or []):
            if c.get("enable") is False or not tag_re.search(c.get("comment") or ""):
                continue
            email = c.get("email", "")
            if not NAME_RE.match(email):
                skipped.append(email)
                continue
            params = [("encryption", "none")]
            if c.get("flow"):
                params.append(("flow", c["flow"]))
            params += [
                ("fp", rs.get("fingerprint") or "chrome"),
                ("pbk", rs.get("publicKey", "")),
                ("security", "reality"),
                ("sid", (r.get("shortIds") or [""])[0]),
                ("sni", (r.get("serverNames") or [""])[0]),
                ("spx", rs.get("spiderX") or "/"),
                ("type", "tcp"),
            ]
            qs = "&".join(f"{k}={_q(v)}" for k, v in params)
            # метка как у 3x-ui: «название inbound-Email» (например vlessDE-mama_home_router)
            label = f"{(remark or '').strip() or f'slot{slot}'}-{email}"
            links[email] = f"vless://{c['id']}@{host}:{port}?{qs}#{_q(label)}"
    return links, skipped


# ---------- сбор со всех серверов ----------

@dataclass
class Collected:
    per_slot: dict[int, dict[str, str]] = field(default_factory=dict)   # успешно собранные слоты
    failed: dict[int, str] = field(default_factory=dict)                # слот -> причина
    names: dict[int, str] = field(default_factory=dict)                 # слот -> имя сервера
    skipped: dict[int, list[str]] = field(default_factory=dict)         # слот -> неподходящие имена


async def collect_server(srv: storage.Server) -> tuple[dict[str, str], list[str]]:
    with tempfile.TemporaryDirectory() as tmp:
        async with ssh.connect(srv) as conn:
            path = await ssh.fetch_xui_db(conn, tmp)
        if not path:
            raise ssh.SSHError("на сервере нет базы 3x-ui")
        return parse_xui_db(path, srv.host, srv.slot)


async def collect() -> Collected:
    servers = [s for s in storage.load() if s.slot is not None]
    res = Collected()
    results = await asyncio.gather(*(collect_server(s) for s in servers), return_exceptions=True)
    for srv, r in zip(servers, results):
        res.names[srv.slot] = srv.name
        if isinstance(r, Exception):
            res.failed[srv.slot] = str(r)
        else:
            res.per_slot[srv.slot], res.skipped[srv.slot] = r
    return res


# ---------- план изменений ----------

SLOT_LINE = re.compile(r"^#\s*slot\s*(\d+)\s*$", re.I)


def parse_file(lines: list[str]) -> dict[int, str]:
    """Файл роутера -> {слот: ссылка}. Слот задаёт строка «# slot N» перед ссылкой;
    в старом формате (без таких строк) — метка #slotN- в самой ссылке."""
    out, cur = {}, None
    for ln in lines:
        if m := SLOT_LINE.match(ln):
            cur = int(m.group(1))
        elif ln.startswith("vless://"):
            s = cur
            if s is None and (m := re.search(r"#slot(\d+)-", ln)):
                s = int(m.group(1))
            if s is not None:
                out.setdefault(s, ln)
            cur = None
    return out


def render_file(by_slot: dict[int, str]) -> list[str]:
    lines = []
    for s in sorted(by_slot):
        lines += [f"# slot {s}", by_slot[s]]
    return lines


@dataclass
class Plan:
    files: dict[str, str | None] = field(default_factory=dict)  # путь -> новое содержимое (None — удалить)
    added: list[str] = field(default_factory=list)
    changed: dict[str, list[int]] = field(default_factory=dict)  # роутер -> изменившиеся слоты
    removed: list[str] = field(default_factory=list)
    kept: dict[str, list[int]] = field(default_factory=dict)     # роутер -> слоты, где оставлена старая ссылка
    missing: dict[int, list[str]] = field(default_factory=dict)  # слот -> роутеры, которых нет на этом сервере
    legacy: list[str] = field(default_factory=list)              # старые slotN.txt к удалению
    total: int = 0

    @property
    def empty(self) -> bool:
        return not self.files


def make_plan(col: Collected, old: dict[str, list[str]], legacy: list[str] = ()) -> Plan:
    """old — текущее содержимое GitHub: {имя роутера: [ссылки]}."""
    plan = Plan(legacy=list(legacy))
    slots = sorted(set(col.per_slot) | set(col.failed))
    now = {n for links in col.per_slot.values() for n in links}

    for name in sorted(now | set(old)):
        old_by_slot = parse_file(old.get(name, []))
        present = name in now
        if not present and col.failed and old.get(name):
            # роутера нет на доступных серверах, но часть серверов не ответила — не трогаем
            plan.total += 1
            continue
        if not present:
            plan.removed.append(name)
            plan.files[f"{DIR}/{name}.txt"] = None
            continue

        new_by_slot, kept = {}, []
        for s in slots:
            if s in col.per_slot and name in col.per_slot[s]:
                new_by_slot[s] = col.per_slot[s][name]
            elif s in old_by_slot:  # сервер недоступен или на нём нет роутера — оставляем прежнюю ссылку
                new_by_slot[s] = old_by_slot[s]
                kept.append(s)
            if s in col.per_slot and name not in col.per_slot[s]:
                plan.missing.setdefault(s, []).append(name)
        plan.total += 1
        if kept:
            plan.kept[name] = kept
        lines = render_file(new_by_slot)
        if name not in old:
            plan.added.append(name)
        elif lines != old[name]:
            plan.changed[name] = sorted(s for s in set(new_by_slot) | set(old_by_slot)
                                        if new_by_slot.get(s) != old_by_slot.get(s))
        else:
            continue
        plan.files[f"{DIR}/{name}.txt"] = "\n".join(lines) + "\n"

    for p in plan.legacy:
        plan.files[p] = None
    return plan


def plan_text(col: Collected, plan: Plan) -> str:
    """Человекочитаемое описание для Telegram (HTML)."""
    from html import escape

    def e(s):
        return escape(str(s), quote=False)
    out = []
    for s in sorted(set(col.per_slot) | set(col.failed)):
        nm = e(col.names.get(s, "?"))
        if s in col.failed:
            out.append(f"🔴 Слот {s} · {nm}: {e(col.failed[s])}")
        else:
            out.append(f"🟢 Слот {s} · {nm}: роутеров {len(col.per_slot[s])}")
    if not col.per_slot and not col.failed:
        return "Нет серверов со слотами. Добавь сервер и назначь ему слот."
    out.append("")
    if plan.added:
        out += [f"  + {e(n)}" for n in plan.added]
    if plan.changed:
        out += [f"  ~ {e(n)} ({'слот ' + ', '.join(map(str, sl)) if sl else 'формат файла'})" for n, sl in plan.changed.items()]
    if plan.removed:
        out += [f"  − {e(n)} (нет ни на одном сервере)" for n in plan.removed]
    if plan.legacy:
        out.append(f"  🧹 удалю старые файлы: {e(', '.join(plan.legacy))}")
    if plan.empty:
        out.append("✅ В GitHub всё актуально")
    for s, names in sorted(plan.missing.items()):
        out.append(f"⚠️ На слоте {s} нет: {e(', '.join(names))}")
    for name, sl in plan.kept.items():
        out.append(f"ℹ️ {e(name)}: для слота {', '.join(map(str, sl))} оставлена прежняя ссылка")
    for s, names in sorted(col.skipped.items()):
        if names:
            out.append(f"⚠️ Слот {s}: пропущены имена с недопустимыми символами: {e(', '.join(names))} "
                       "(можно латиницу, цифры, точку, дефис, подчёркивание)")
    return "\n".join(out)
