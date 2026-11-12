#!/bin/sh
# Установка автообновления ссылок на роутер OpenWrt с podkop:
#   sh -c "$(curl -fsSL https://raw.githubusercontent.com/Kernel1000/podkop-links/main/router/install.sh)"
# Без вопросов: NAME=Irinka_home_router GH_TOKEN=github_pat_... sh -c "$(curl ...)"
CODE_REPO="${CODE_REPO:-Kernel1000/podkop-links}"
RAW="https://raw.githubusercontent.com/$CODE_REPO/main/router"
CONF=/etc/podkop-links.conf
BIN=/usr/bin/podkop-links-sync

die() { echo "❌ $*" >&2; exit 1; }

# значения из окружения важнее сохранённых
E_NAME="$NAME" E_REPO="$GH_REPO" E_TOKEN="$GH_TOKEN"
# shellcheck disable=SC1090
[ -f "$CONF" ] && . "$CONF"
NAME="${E_NAME:-$NAME}" GH_REPO="${E_REPO:-${GH_REPO:-${CODE_REPO%%/*}/router-links}}" GH_TOKEN="${E_TOKEN:-$GH_TOKEN}"

uci -q get podkop.main >/dev/null || die "podkop не найден (нет секции podkop.main)"

ver=$( (apk list -I 2>/dev/null; opkg list-installed 2>/dev/null) \
       | sed -n 's/^podkop - \([^ ]*\).*/podkop \1/p; s/^podkop-\([0-9][^ ]*\) .*/podkop \1/p' | head -n1 )
echo "==> ${ver:-podkop (версия не определена)}"

if ! command -v curl >/dev/null; then
  echo "==> Ставлю curl"
  if command -v apk >/dev/null; then
    { apk update && apk add curl; } >/dev/null 2>&1 || die "Не удалось поставить curl (apk)"
  else
    { opkg update && opkg install curl; } >/dev/null 2>&1 || die "Не удалось поставить curl (opkg)"
  fi
fi

echo "==> Скачиваю podkop-links-sync"
NEWBIN=/tmp/podkop-links-sync.$$
curl -fsSL --max-time 30 "$RAW/podkop-links-sync" -o "$NEWBIN" || die "Не удалось скачать"
sh -n "$NEWBIN" || { rm -f "$NEWBIN"; die "Скачанный скрипт повреждён"; }

# режим podkop: urltest — ок; одна ссылка (url) — переключим после проверки ссылок; иначе — стоп, ничего не меняя
st=$(sh "$NEWBIN" --check-mode); rc=$?
echo "==> $st"
case $rc in
  0) SW="" ;;
  2) SW="--switch-mode" ;;
  *) rm -f "$NEWBIN"; die "Установка остановлена, на роутере ничего не изменено." ;;
esac
mv "$NEWBIN" "$BIN" && chmod 755 "$BIN"

ask() { # ask <вопрос> <по умолчанию>
  if [ -n "$2" ]; then printf '%s [%s]: ' "$1" "$2" >/dev/tty; else printf '%s: ' "$1" >/dev/tty; fi
  read -r v </dev/tty; echo "${v:-$2}"
}

if [ -z "$E_NAME" ] || [ -z "$GH_TOKEN" ]; then
  cur=$(uci -q get podkop.main.urltest_proxy_links)
  [ -n "$cur" ] || cur=$(uci -q get podkop.main.proxy_string)
  [ -n "$cur" ] && { echo "Сейчас в podkop:"; for l in $cur; do case "$l" in vless://*) echo "  #${l##*#}" ;; esac; done; }
  while :; do
    NAME=$(ask "Имя роутера (Email клиента в 3x-ui, одинаковый на всех серверах)" "$NAME")
    [ -n "$NAME" ] && break; echo "Имя не может быть пустым" >/dev/tty
  done
  GH_REPO=$(ask "GitHub-репозиторий со ссылками" "$GH_REPO")
  while :; do
    if [ -n "$GH_TOKEN" ]; then t=$(ask "GitHub-токен только на чтение (Enter — оставить сохранённый)" "")
    else t=$(ask "GitHub-токен только на чтение" ""); fi
    [ -n "$t" ] && GH_TOKEN="$t"
    [ -n "$GH_TOKEN" ] && break; echo "Токен не может быть пустым" >/dev/tty
  done
fi
[ -n "$NAME" ] && [ -n "$GH_TOKEN" ] || die "Нужны имя роутера и токен"

umask 077
printf "NAME='%s'\nGH_REPO='%s'\nGH_TOKEN='%s'\n" "$NAME" "$GH_REPO" "$GH_TOKEN" > "$CONF"

echo "==> Проверка"
# shellcheck disable=SC2086
"$BIN" --dry $SW || die "Проверь имя, репозиторий и токен. Режим podkop и ссылки не менялись."

echo "==> Применяю"
# shellcheck disable=SC2086
"$BIN" --force $SW || die "Не удалось применить, режим podkop и ссылки не менялись. Лог: logread -e podkop-links"

echo "==> cron: каждые 10 минут"
crontab -l 2>/dev/null | grep -v podkop-links-sync > /tmp/cron.$$
echo "*/10 * * * * $BIN" >> /tmp/cron.$$
crontab /tmp/cron.$$ && rm -f /tmp/cron.$$
/etc/init.d/cron enable >/dev/null 2>&1; /etc/init.d/cron restart >/dev/null 2>&1

echo "✅ Готово. Лог: logread -e podkop-links"
