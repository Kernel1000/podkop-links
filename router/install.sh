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

# версия podkop — для информации; режим должен быть urltest, иначе ссылки запишутся «мимо»
ver=$( (apk list -I 2>/dev/null; opkg list-installed 2>/dev/null) \
       | sed -n 's/^podkop - \([^ ]*\).*/podkop \1/p; s/^podkop-\([0-9][^ ]*\) .*/podkop \1/p' | head -n1 )
mode=$(uci -q get podkop.main.proxy_config_type)
echo "==> ${ver:-podkop (версия не определена)}, режим: ${mode:-не задан}"
if [ "$mode" != urltest ]; then
  die "podkop работает не в режиме urltest, а скрипт пишет ссылки именно туда (urltest_proxy_links).
   Переключи в LuCI: Services → Podkop → тип конфигурации прокси → URLTest и запусти установку снова.
   Если такого пункта нет — podkop слишком старый, сначала обнови его."
fi

if ! command -v curl >/dev/null; then
  echo "==> Ставлю curl"
  if command -v apk >/dev/null; then
    { apk update && apk add curl; } >/dev/null 2>&1 || die "Не удалось поставить curl (apk)"
  else
    { opkg update && opkg install curl; } >/dev/null 2>&1 || die "Не удалось поставить curl (opkg)"
  fi
fi

echo "==> Скачиваю podkop-links-sync"
curl -fsSL --max-time 30 "$RAW/podkop-links-sync" -o "$BIN.new" && sh -n "$BIN.new" || die "Не удалось скачать"
mv "$BIN.new" "$BIN" && chmod 755 "$BIN"

ask() { # ask <вопрос> <по умолчанию>
  if [ -n "$2" ]; then printf '%s [%s]: ' "$1" "$2" >/dev/tty; else printf '%s: ' "$1" >/dev/tty; fi
  read -r v </dev/tty; echo "${v:-$2}"
}

if [ -z "$E_NAME" ] || [ -z "$GH_TOKEN" ]; then
  cur=$(uci -q get podkop.main.urltest_proxy_links)
  [ -n "$cur" ] && { echo "Сейчас в podkop:"; for l in $cur; do echo "  #${l##*#}"; done; }
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
"$BIN" --dry || die "Проверь имя, репозиторий и токен"

echo "==> cron: каждые 10 минут"
crontab -l 2>/dev/null | grep -v podkop-links-sync > /tmp/cron.$$
echo "*/10 * * * * $BIN" >> /tmp/cron.$$
crontab /tmp/cron.$$ && rm -f /tmp/cron.$$
/etc/init.d/cron enable >/dev/null 2>&1; /etc/init.d/cron restart >/dev/null 2>&1

echo "==> Применяю"
"$BIN"
echo "✅ Готово. Лог: logread -e podkop-links"
