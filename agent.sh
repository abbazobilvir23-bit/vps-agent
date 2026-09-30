#!/usr/bin/env bash
# Отправляет задачу агенту и печатает ответ.
#
#   bash agent.sh "задача"              новая сессия
#   bash agent.sh --continue "задача"    продолжить последнюю
#   bash agent.sh --session <id> "..."   продолжить конкретную
#   bash agent.sh --new "задача"         принудительно новая сессия
#   bash agent.sh --status               показать состояние
set -euo pipefail

ROOT="${AGENT_ROOT:-/root/agent}"
ENVF="$ROOT/env"
WS="$ROOT/workspace"
SESSION="$ROOT/session"
OC="${OPENCODE_BIN:-$HOME/.opencode/bin/opencode}"
MODEL="openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"
TIMEOUT="${AGENT_TIMEOUT:-1800}"

[ -x "$OC" ] || OC="$(command -v opencode || true)"
[ -x "$OC" ] || { echo "opencode не найден — выполни setup.sh" >&2; exit 1; }
[ -f "$ENVF" ] || { echo "нет файла секретов $ENVF — выполни setup.sh" >&2; exit 1; }

set -a; . "$ENVF"; set +a
[ -n "${OPENROUTER_API_KEY:-}" ] || { echo "OPENROUTER_API_KEY пуст в $ENVF" >&2; exit 1; }

MODE="new"; SID=""
while [ $# -gt 0 ]; do
  case "$1" in
    --continue|-c) MODE="continue"; shift ;;
    --new|-n)      MODE="new"; shift ;;
    --session|-s)  MODE="session"; SID="$2"; shift 2 ;;
    --status)      MODE="status"; shift ;;
    *) break ;;
  esac
done
MSG="${1:-}"

if [ "$MODE" = "status" ]; then
  echo "модель:    $MODEL"
  echo "рабочая:   $WS"
  echo "сессия:    $(cat "$SESSION" 2>/dev/null || echo '—')"
  echo "баланс OpenRouter: неизвестен offline, см. https://openrouter.ai/credits"
  cd "$WS"; git log --oneline -5 2>/dev/null || true
  exit 0
fi

[ -n "$MSG" ] || { echo "usage: bash agent.sh [--continue|--new|--session ID] \"задача\"" >&2; exit 2; }

cd "$WS"
# v1: --pure вместо --standalone; v2: --standalone. Определяем по версии.
OC_MAJOR="$("$OC" --version 2>/dev/null | tr -dc '0-9' | cut -c1)"
if [ "${OC_MAJOR:-1}" -ge 2 ] 2>/dev/null; then
  ARGS=(--standalone --auto --format json --model "$MODEL")
else
  ARGS=(--pure --auto --format json --model "$MODEL")
fi
case "$MODE" in
  continue) [ -s "$SESSION" ] && ARGS+=(--session "$(cat "$SESSION")") || echo "(сессии нет — начну новую)" >&2 ;;
  session)  ARGS+=(--session "$SID") ;;
esac

OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

echo "▶ задача: $MSG" >&2
set +e
timeout --signal=INT "$TIMEOUT" "$OC" run "${ARGS[@]}" "$MSG" >"$OUT" 2>>"$ROOT/agent.err"
RC=$?
set -e

# сохраняем/обновляем id сессии
NEWID="$(grep -o '"sessionID":"ses_[A-Za-z0-9]*"' "$OUT" 2>/dev/null | head -1 | cut -d'"' -f4 || true)"
if [ -n "$NEWID" ]; then printf '%s' "$NEWID" > "$SESSION"; chmod 600 "$SESSION"; fi

# печатаем только текст ответа + факт записи файлов
python3 - "$OUT" <<'PY'
import json, sys
out = sys.argv[1]
texts, edits = [], []
for line in open(out, encoding="utf-8", errors="ignore"):
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        d = json.loads(line)
    except Exception:
        continue
    p = d.get("part", {}) or {}
    if p.get("type") == "text" and p.get("text"):
        texts.append(p["text"])
    if p.get("type") == "tool" and p.get("tool") in ("edit", "write", "patch"):
        st = p.get("state", {}) or {}
        inp = st.get("input", {}) or {}
        path = (inp.get("filePath") or inp.get("path") or inp.get("file")
                or st.get("title") or st.get("metadata", {}).get("path") or "")
        edits.append(f"{p.get('tool')}: {path}")
if texts:
    print(texts[-1].strip())
else:
    print("(агент не вернул текстового ответа — смотри agent.err)")
if edits:
    print("\nИзменённые файлы:", file=sys.stderr)
    for e in dict.fromkeys(edits):
        print("  -", e[:160], file=sys.stderr)
PY

# автокоммит, чтобы был откат
if [ -n "$(git status --porcelain 2>/dev/null || true)" ]; then
  git add -A
  git commit -q -m "agent: ${MSG:0:60}" || true
  echo "✓ закоммичено: $(git rev-parse --short HEAD)" >&2
  git --no-pager diff --stat HEAD~1 HEAD 2>/dev/null | tail -n +1 >&2 || true
else
  echo "✓ изменений не было" >&2
fi

[ "$RC" -eq 0 ] || echo "⚠ opencode завершился с кодом $RC (см. $ROOT/agent.err)" >&2
exit 0
