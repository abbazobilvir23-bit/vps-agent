#!/usr/bin/env bash
# Управление Telegram-мостом. Не трогает systemd, /etc и сеть.
#
#   bash run.sh start      запустить
#   bash run.sh stop       остановить
#   bash run.sh restart    перезапустить
#   bash run.sh status     проверить
#   bash run.sh keepalive  запустить, если не работает (для cron)
#   bash run.sh log        последние строки лога
#   bash run.sh check      проверить конфигурацию
#   bash run.sh cron       поставить сторож в crontab
set -euo pipefail

ROOT="${AGENT_ROOT:-/root/agent}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDF="$ROOT/bridge.pid"
LOG="$ROOT/bridge.log"

ok()  { printf '\033[1;32m[ок]\033[0m %s\n' "$1"; }
bad() { printf '\033[1;31m[стоп]\033[0m %s\n' "$1"; }
say() { printf '%s\n' "$1"; }

running() {
  [ -f "$PIDF" ] || return 1
  local pid; pid="$(cat "$PIDF" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

start() {
  if running; then
    ok "мост уже работает (pid $(cat "$PIDF"))"
    return 0
  fi
  command -v python3 >/dev/null 2>&1 || { bad "нет python3 на сервере"; return 1; }
  mkdir -p "$ROOT"
  chmod 700 "$ROOT" 2>/dev/null || true
  nohup python3 "$HERE/bot.py" >>"$ROOT/bridge.out" 2>&1 &
  echo $! > "$PIDF"
  sleep 2
  if running; then
    ok "мост запущен (pid $(cat "$PIDF")), лог: $LOG"
  else
    bad "не удалось запустить. Последние строки лога:"
    tail -n 15 "$ROOT/bridge.out" >&2 || true
    rm -f "$PIDF"
    return 1
  fi
}

stop() {
  if ! running; then
    say "мост не работает"
    rm -f "$PIDF"
    return 0
  fi
  local pid; pid="$(cat "$PIDF")"
  kill -TERM "$pid" 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
  rm -f "$PIDF"
  # убираем возможные осиротевшие дубли (по пути к скрипту, не по имени)
  pkill -f "$HERE/bot.py" 2>/dev/null || true
  sleep 1
  if pgrep -f "$HERE/bot.py" >/dev/null 2>&1; then
    pkill -9 -f "$HERE/bot.py" 2>/dev/null || true
  fi
  ok "мост остановлен"
}

case "${1:-status}" in
  start)    start ;;
  stop)     stop ;;
  restart)  stop; sleep 1; start ;;
  keepalive) running || start ;;
  status)
    if running; then
      ok "работает (pid $(cat "$PIDF"))"
      python3 "$HERE/bot.py" --check || true
    else
      bad "не работает"
      exit 1
    fi ;;
  log)      n="${2:-40}"
            echo "--- $LOG ---";        tail -n "$n" "$LOG" 2>/dev/null || echo "(пусто)"
            echo "--- $ROOT/bridge.out (ошибки Python) ---"
            tail -n "$n" "$ROOT/bridge.out" 2>/dev/null || echo "(пусто)" ;;
  check)    python3 "$HERE/bot.py" --check ;;
  cron)
    LINE="* * * * * $HERE/run.sh keepalive >/dev/null 2>&1"
    if crontab -l 2>/dev/null | grep -qF "run.sh keepalive"; then
      ok "сторож уже есть в crontab"
    else
      ( crontab -l 2>/dev/null; echo "$LINE" ) | crontab -
      ok "сторож добавлен: каждую минуту проверяет, жив ли мост"
    fi
    crontab -l 2>/dev/null | grep -F "run.sh keepalive" ;;
  *)
    say "usage: bash run.sh {start|stop|restart|status|keepalive|log|check|cron}"
    exit 2 ;;
esac
