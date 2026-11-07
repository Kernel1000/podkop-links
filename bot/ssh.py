"""SSH: установка ключа бота на сервер, подключение, сведения о сервере, скачивание базы 3x-ui."""
import os
import shlex

import asyncssh

from config import KNOWN_HOSTS, SSH_KEY, XUI_DB
from storage import Server

TIMEOUT = 15


class SSHError(Exception):
    pass


def ensure_key() -> None:
    """Создать SSH-ключ бота, если его ещё нет."""
    if os.path.exists(SSH_KEY):
        return
    key = asyncssh.generate_private_key("ssh-ed25519", comment="podkop-bot")
    key.write_private_key(SSH_KEY)
    key.write_public_key(SSH_KEY + ".pub")
    os.chmod(SSH_KEY, 0o600)


def public_key() -> str:
    with open(SSH_KEY + ".pub", encoding="utf-8") as f:
        return f.read().strip()


def _kh_id(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def _remember_host_key(host: str, port: int, key: asyncssh.SSHKey) -> None:
    """Запомнить ключ сервера (если сервер переустановили — старая запись заменяется)."""
    hid = _kh_id(host, port)
    k = key.export_public_key("openssh").decode().split()
    line = f"{hid} {k[0]} {k[1]}\n"
    try:
        with open(KNOWN_HOSTS, encoding="utf-8") as f:
            lines = [ln for ln in f if ln.split(" ", 1)[0] != hid]
    except FileNotFoundError:
        lines = []
    with open(KNOWN_HOSTS, "w", encoding="utf-8") as f:
        f.writelines(lines + [line])


def _human(e: Exception) -> str:
    if isinstance(e, asyncssh.PermissionDenied):
        return "неверный пароль или вход для root запрещён"
    if isinstance(e, asyncssh.HostKeyNotVerifiable):
        return "ключ сервера изменился (сервер переустановили?) — добавь его заново"
    if isinstance(e, (TimeoutError, OSError)):
        return f"сервер не отвечает ({e.__class__.__name__}: {e})"
    return f"{e.__class__.__name__}: {e}"


async def install_key(host: str, port: int, password: str) -> None:
    """Войти по паролю, добавить ключ бота в authorized_keys, проверить вход по ключу."""
    ensure_key()
    pub = public_key()
    cmd = ("umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
           f"(grep -qxF {shlex.quote(pub)} ~/.ssh/authorized_keys || echo {shlex.quote(pub)} >> ~/.ssh/authorized_keys)")
    try:
        async with asyncssh.connect(host, port=port, username="root", password=password,
                                    client_keys=None, known_hosts=None, connect_timeout=TIMEOUT,
                                    preferred_auth="password,keyboard-interactive") as c:
            hk = c.get_server_host_key()
            await c.run(cmd, check=True, timeout=TIMEOUT)
    except Exception as e:  # noqa: BLE001
        raise SSHError(_human(e)) from e
    _remember_host_key(host, port, hk)
    # проверяем, что теперь пускает по ключу
    async with connect(Server("check", host, port)) as c:
        await c.run("true", check=True, timeout=TIMEOUT)


class connect:
    """async with connect(server) as conn: ... — вход по ключу бота."""

    def __init__(self, srv: Server):
        self.srv = srv
        self.conn = None

    async def __aenter__(self) -> asyncssh.SSHClientConnection:
        try:
            self.conn = await asyncssh.connect(
                self.srv.host, port=self.srv.port, username="root",
                client_keys=[SSH_KEY], known_hosts=KNOWN_HOSTS if os.path.exists(KNOWN_HOSTS) else (),
                connect_timeout=TIMEOUT, preferred_auth="publickey")
        except Exception as e:  # noqa: BLE001
            raise SSHError(_human(e)) from e
        return self.conn

    async def __aexit__(self, *exc):
        self.conn.close()
        await self.conn.wait_closed()


INFO_CMD = r"""
echo "hostname=$(hostname)"
. /etc/os-release 2>/dev/null; echo "os=$PRETTY_NAME"
[ -f %(db)s ] && echo xui=1 || echo xui=0
echo "xui_active=$(systemctl is-active x-ui 2>/dev/null)"
x=$(ls /usr/local/x-ui/bin/xray-linux-* 2>/dev/null | head -n1)
[ -n "$x" ] && echo "xray=$("$x" version 2>/dev/null | head -n1 | awk '{print $2}')"
true
""" % {"db": XUI_DB}


async def info(conn: asyncssh.SSHClientConnection) -> dict:
    r = await conn.run(INFO_CMD, timeout=TIMEOUT)
    return dict(ln.split("=", 1) for ln in (r.stdout or "").splitlines() if "=" in ln)


async def fetch_xui_db(conn: asyncssh.SSHClientConnection, dest_dir: str) -> str | None:
    """Скачать базу 3x-ui (и её WAL-журнал, если есть). Возвращает локальный путь или None."""
    local = os.path.join(dest_dir, "x-ui.db")
    async with conn.start_sftp_client() as sftp:
        if not await sftp.exists(XUI_DB):
            return None
        for suffix in ("", "-wal"):
            if await sftp.exists(XUI_DB + suffix):
                await sftp.get(XUI_DB + suffix, local + suffix)
    return local
