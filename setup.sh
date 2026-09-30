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
cd "$WS"
if [ ! -d .git ]; then
  git init -q
  git config user.email "agent@localhost"
  git config user.name  "vps-agent"
fi
if [ -n "$(git status --porcelain 2>/dev/null || true)" ]; then
  git add -A
  git commit -q -m "песочница: базовая настройка" || true
fi
echo "   репозиторий: $(git rev-parse --short HEAD 2>/dev/null || echo 'пусто')"

say "5/6 Секреты ($ENVF, права 600)"
if [ ! -f "$ENVF" ]; then
  echo "   Сейчас спроси OpenRouter API key. Он не попадёт в git и не в историю shell."
  read -rsp "   Вставь OPENROUTER_API_KEY и нажми Enter: " KEY; echo
  if [ -z "$KEY" ]; then
    warn "Ключ не введён — агент не сможет работать. Потом заполни руками: $ENVF"
  fi
  {
    [ -n "${KEY:-}" ] && echo "OPENROUTER_API_KEY=$KEY"
    echo "# сюда позже добавим TELEGRAM_BOT_TOKEN (фаза 2)"
    echo "TELEGRAM_BOT_TOKEN="
  } > "$ENVF"
  chmod 600 "$ENVF"
  unset KEY
  echo "   записано"
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
cat <<EOF

Агент установлен. Что где:
  $OC                  — сам OpenCode
  $WS            — папка-песочница (git-репозиторий агента)
  $WS/opencode.json  — правила доступа (песочница, запреты)
  $WS/AGENTS.md       — инструкция агенту
  $ENVF            — секреты (600)
  $SESSION         — id текущей сессии

Дальше:
  1) Проверь безопасность:   bash $APP/smoke.sh
  2) Первая задача:          bash $APP/agent.sh "Создай README.md с описанием проекта"
  3) Продолжить диалог:      bash $APP/agent.sh --continue "Теперь добавь раздел установки"
  4) Новая сессия:           bash $APP/agent.sh --new "Другая задача"

Торговый бот в /root/mpf агент не видит и не может изменить.

EOF
