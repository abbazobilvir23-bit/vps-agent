#!/usr/bin/env bash
# Установка агента на VPS. Идемпотентно: можно запускать повторно.
#
# НЕ трогает: docker, systemctl, сеть, /etc, .bashrc, торгового бота в /root/mpf.
# Кладёт только в ~/.opencode/bin и /root/agent.
set -euo pipefail

ROOT="${AGENT_ROOT:-/root/agent}"
APP="$ROOT/app"
WS="$ROOT/workspace"
ENVF="$ROOT/env"
SESSION="$ROOT/session"
OC_VERSION="2.0.18"
MODEL_DEFAULT="openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$1"; }
die() { printf '\033[1;31m[x] %s\033[0m\n' "$1" >&2; exit 1; }

if [ "$(id -u)" != "0" ] && [ -z "${AGENT_ROOT:-}" ]; then
  die "Запускай от root: bash setup.sh"
fi

say "1/6 Каталоги"
mkdir -p "$ROOT" "$APP" "$WS"
chmod 700 "$ROOT"
echo "   $ROOT готов"

say "2/6 OpenCode $OC_VERSION"
OC=""
if [ -n "${OPENCODE_BIN:-}" ] && [ -x "$OPENCODE_BIN" ]; then
  OC="$OPENCODE_BIN"
  echo "   задан OPENCODE_BIN: $OC ($("$OC" --version))"
elif [ -x "$HOME/.opencode/bin/opencode" ] && "$HOME/.opencode/bin/opencode" --version 2>/dev/null | grep -q "$OC_VERSION"; then
  OC="$HOME/.opencode/bin/opencode"
  echo "   уже установлен: $("$OC" --version)"
else
  curl -fsSL https://opencode.ai/install | bash -s -- --version "$OC_VERSION" --no-modify-path
  OC="$HOME/.opencode/bin/opencode"
fi
[ -x "$OC" ] || OC="$(command -v opencode || true)"
[ -x "$OC" ] || die "opencode не установился"
echo "   бинарь: $OC ($("$OC" --version))"

say "3/6 Конфигурация агента в $WS"
# Конфиг кладём в рабочую папку (project config), а не глобально —
# так мы вообще не трогаем настройки других инструментов на сервере.
cp -f "$APP/workspace/opencode.json" "$WS/opencode.json"
cp -f "$APP/workspace/AGENTS.md"    "$WS/AGENTS.md"
echo "   opencode.json и AGENTS.md скопированы"

say "4/6 Git-репозиторий песочницы"
# На этом VPS git ругается "dubious ownership" (та же история, что была с /root/mpf).
# Разрешаем каталог явно и НЕ роняем установку, если git всё-таки споткнёлся.
git config --global --add safe.directory "$WS" 2>/dev/null || true
git config --global --add safe.directory "$APP" 2>/dev/null || true
cd "$WS" || die "не удалось перейти в $WS"
if [ ! -d .git ]; then
  git init -q 2>/dev/null || warn "git init не удался (продолжаю)"
fi
git config user.email "agent@localhost" 2>/dev/null || true
git config user.name  "vps-agent"       2>/dev/null || true
if git status --porcelain >/dev/null 2>&1 && [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  git add -A 2>/dev/null || true
  git commit -q -m "песочница: базовая настройка" 2>/dev/null || true
fi
if git rev-parse --short HEAD >/dev/null 2>&1; then
  echo "   репозиторий: $(git rev-parse --short HEAD)"
else
  warn "git в песочнице не работает — агент сможет писать файлы, но без истории/отката"
fi

say "5/6 Секреты ($ENVF, права 600)"
if [ ! -f "$ENVF" ]; then
  if [ -t 0 ]; then
    echo "   Сейчас спроси OpenRouter API key. Он не отображается при вводе и не попадёт"
    echo "   ни в git, ни в историю shell. Вставь ключ и нажми Enter."
    read -rsp "   OpenRouter API key: " KEY; echo
  else
    warn "нет TTY (вставка нескольких команд сразу). Создаю файл без ключа."
    KEY=""
  fi
  if [ -z "${KEY:-}" ]; then
    warn "Ключ пуст. Впиши его одной командой:"
    echo "     sed -i 's|^OPENROUTER_API_KEY=.*|OPENROUTER_API_KEY=ТВОЙ_КЛЮЧ|' $ENVF"
  fi
  {
    echo "OPENROUTER_API_KEY=${KEY:-}"
    echo "# Фаза 2: сюда добавим TELEGRAM_BOT_TOKEN"
    echo "TELEGRAM_BOT_TOKEN="
  } > "$ENVF"
  chmod 600 "$ENVF"
  unset KEY
  echo "   записано в $ENVF (права 600)"
else
  echo "   уже существует, не трогаю"
fi

say "6/6 Переменные запуска"
cat > "$ROOT/agent.env" <<EOF
ROOT=$ROOT
WS=$WS
ENVF=$ENVF
SESSION=$SESSION
OC=$OC
MODEL=$MODEL_DEFAULT
EOF
chmod 600 "$ROOT/agent.env"
[ -f "$SESSION" ] || : > "$SESSION"
chmod 600 "$SESSION"

say "Готово"

# --- проверки, чтобы не гадать ---
KEYSET="нет"
if grep -qE '^OPENROUTER_API_KEY=.+' "$ENVF" 2>/dev/null; then KEYSET="да"; fi
GITOK="нет"; cd "$WS" 2>/dev/null && git rev-parse --short HEAD >/dev/null 2>&1 && GITOK="да"

cat <<EOF

Проверка установки:
  opencode:   $("$OC" --version 2>/dev/null || echo "НЕ РАБОТАЕТ")
  opencode:   $OC
  ключ API:   $KEYSET
  git в песочнице: $GITOK
  песочница:  $WS
  секреты:    $ENVF

Дальше (по одной команде, НЕ вставляй всё сразу):
  1) bash $APP/smoke.sh
  2) bash $APP/agent.sh "Создай README.md с описанием проекта"
  3) bash $APP/agent.sh --continue "Теперь добавь раздел установки"
  4) bash $APP/agent.sh --new "Другая задача"

Если захочешь вызывать opencode напрямую, добавь в PATH:
  export PATH="\$HOME/.opencode/bin:\$PATH"

Торговый бот в /root/mpf агент не видит и не может изменить.
EOF
