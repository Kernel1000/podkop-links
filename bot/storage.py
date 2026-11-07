"""Список серверов: /etc/podkop-bot/servers.json."""
import json
import os
import tempfile
from dataclasses import asdict, dataclass

from config import SERVERS_FILE


@dataclass
class Server:
    name: str
    host: str
    port: int = 22
    slot: int | None = None  # None — резерв, ссылки с него не выгружаются

    @property
    def addr(self) -> str:
        return self.host if self.port == 22 else f"{self.host}:{self.port}"


def load() -> list[Server]:
    try:
        with open(SERVERS_FILE, encoding="utf-8") as f:
            return [Server(**s) for s in json.load(f).get("servers", [])]
    except FileNotFoundError:
        return []


def save(servers: list[Server]) -> None:
    servers = sorted(servers, key=lambda s: (s.slot is None, s.slot or 0, s.name))
    data = {"servers": [asdict(s) for s in servers]}
    d = os.path.dirname(SERVERS_FILE)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".servers.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SERVERS_FILE)


def get(name: str) -> Server | None:
    return next((s for s in load() if s.name == name), None)


def by_host(host: str, port: int) -> Server | None:
    return next((s for s in load() if s.host == host and s.port == port), None)


def in_slot(slot: int) -> Server | None:
    return next((s for s in load() if s.slot == slot), None)


def slots() -> list[int]:
    return sorted({s.slot for s in load() if s.slot is not None})


def upsert(server: Server, replace_name: str | None = None) -> None:
    """Добавить или заменить сервер. Если его слот занят другим — тот уходит в резерв."""
    out = []
    for s in load():
        if s.name in (server.name, replace_name):
            continue
        if server.slot is not None and s.slot == server.slot:
            s.slot = None
        out.append(s)
    out.append(server)
    save(out)


def remove(name: str) -> None:
    save([s for s in load() if s.name != name])
