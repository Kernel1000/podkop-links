"""Настройки бота. Берутся из /etc/podkop-bot/config.env (systemd подставляет их в окружение)."""
import os

CONF_DIR = os.environ.get("PODKOP_BOT_DIR", "/etc/podkop-bot")

TG_TOKEN = os.environ.get("TG_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GH_REPO = os.environ.get("GH_REPO", "")          # например Kernel1000/router-links
ROUTER_TAG = os.environ.get("ROUTER_TAG", "router")  # метка в поле Comment клиента 3x-ui

SERVERS_FILE = os.path.join(CONF_DIR, "servers.json")
SSH_KEY = os.path.join(CONF_DIR, "id_ed25519")
KNOWN_HOSTS = os.path.join(CONF_DIR, "known_hosts")

XUI_DB = "/etc/x-ui/x-ui.db"
