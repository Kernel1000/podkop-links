#!/usr/bin/env bash
# Установка / обновление podkop-bot на управляющий сервер:
#   bash <(curl -Ls https://raw.githubusercontent.com/Kernel1000/podkop-links/main/install.sh)
# Повторный запуск — обновление кода, настройки сохраняются.
# Перенастроить (токены, ID): добавь в конце  --reconfigure
set -e
CODE_REPO="${CODE_REPO:-Kernel1000/podkop-links}"
APP=/opt/podkop-bot
CONF_DIR=/etc/podkop-bot
CONF=$CONF_DIR/config.env
UNIT=/etc/systemd/system/podkop-bot.service

die() { echo "❌ $*" >&2; exit 1; }
ask() { local v; read -r -p "$1${2:+ [$2]}: " v </dev/tty; echo "${v:-$2}"; }
[ "$(id -u)" = 0 ] || die "Запусти от root"

echo "==> Зависимости"
apt-get update -qq >/dev/null
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv curl ca-certificates jq >/dev/null

echo "==> Код из $CODE_REPO"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
curl -fsSL "https://codeload.github.com/$CODE_REPO/tar.gz/main" | tar -xz -C "$tmp" || die "Не скачался код"
src=$(find "$tmp" -maxdepth 2 -type d -name bot | head -n1)
[ -f "$src/main.py" ] || die "В архиве нет bot/main.py"
mkdir -p "$APP"
rm -rf "$APP/app.new" && cp -r "$src" "$APP/app.new" && rm -rf "$APP/app" && mv "$APP/app.new" "$APP/app"

echo "==> Python-окружение"
[ -x "$APP/venv/bin/python" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip >/dev/null
"$APP/venv/bin/pip" install -q -r "$APP/app/requirements.txt"

if [ ! -f "$CONF" ] || [ "$1" = "--reconfigure" ]; then
  echo "==> Настройка"
  systemctl stop podkop-bot 2>/dev/null || true
  install -d -m 700 "$CONF_DIR"
  # shellcheck disable=SC1090
  [ -f "$CONF" ] && . "$CONF"

  while :; do
    TG_TOKEN=$(ask "Токен Telegram-бота от @BotFather" "$TG_TOKEN")
    bot=$(curl -fsS "https://api.telegram.org/bot$TG_TOKEN/getMe" 2>/dev/null | jq -r '.result.username // empty')
    [ -n "$bot" ] && { echo "✅ Бот @$bot"; break; }
    echo "❌ Telegram не принял токен"; TG_TOKEN=""
  done

  if [ -z "$ADMIN_IDS" ]; then
    read -r -p "Напиши боту @$bot любое сообщение и нажми Enter… " _ </dev/tty
    ADMIN_IDS=$(curl -fsS "https://api.telegram.org/bot$TG_TOKEN/getUpdates" \
      | jq -r '[.result[].message.from | select(.)] | last | .id // empty')
    [ -n "$ADMIN_IDS" ] && echo "Нашёл твой ID: $ADMIN_IDS"
  fi
  ADMIN_IDS=$(ask "Telegram ID администраторов через запятую" "$ADMIN_IDS")

  GH_REPO=$(ask "Приватный GitHub-репозиторий для ссылок" "${GH_REPO:-${CODE_REPO%%/*}/router-links}")
  while :; do
    t=$(ask "GitHub-токен (Contents: Read and write)${GH_TOKEN:+, Enter — оставить текущий}" "")
    [ -n "$t" ] && GH_TOKEN=$t
    priv=$(curl -fsS -H "Authorization: Bearer $GH_TOKEN" "https://api.github.com/repos/$GH_REPO" 2>/dev/null \
           | jq -r 'if has("private") then .private else empty end')
    if [ "$priv" = true ]; then echo "✅ Доступ к $GH_REPO есть"; break
    elif [ "$priv" = false ]; then echo "⚠️  $GH_REPO публичный — ссылки увидят все! Сделай его приватным."; break; fi
    echo "❌ Нет доступа к $GH_REPO с этим токеном"; GH_TOKEN=""
  done

  umask 077
  printf "TG_TOKEN=%s\nADMIN_IDS=%s\nGH_TOKEN=%s\nGH_REPO=%s\n" "$TG_TOKEN" "$ADMIN_IDS" "$GH_TOKEN" "$GH_REPO" > "$CONF"
  umask 022
fi

echo "==> Сервис"
cat > "$UNIT" <<EOF
[Unit]
Description=podkop-bot (управляющий Telegram-бот)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$APP/app
EnvironmentFile=$CONF
ExecStart=$APP/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable podkop-bot >/dev/null 2>&1
systemctl restart podkop-bot
sleep 3
if systemctl is-active -q podkop-bot; then
  echo "✅ podkop-bot запущен — бот прислал тебе сообщение в Telegram"
else
  echo "❌ Не запустился. Лог:"; journalctl -u podkop-bot -n 20 --no-pager
fi
echo "Лог: journalctl -u podkop-bot -f"
