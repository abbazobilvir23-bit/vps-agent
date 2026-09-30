#!/usr/bin/env bash
# Установка OpenCode с проверкой целостности архива.
#
# Зачем свой скрипт: официальный установщик не проверяет скачанный файл и роняет
# установку, если архив пришёл битым (видели "tar: Error is not recoverable").
#
# Использование: bash install_opencode.sh [версия] [каталог_установки]
set -euo pipefail

VER="${1:-1.18.33}"
DEST="${2:-$HOME/.opencode/bin}"
REPO="anomalyco/opencode"
TIMEOUT="${DL_TIMEOUT:-900}"

say() { printf '   %s\n' "$1"; }

case "$(uname -m)" in
  x86_64|amd64)  arch="x64" ;;
  aarch64|arm64) arch="arm64" ;;
  *) echo "неподдерживаемая архитектура: $(uname -m)" >&2; exit 1 ;;
esac

variant=""
if [ -f /etc/alpine-release ] || { command -v ldd >/dev/null 2>&1 && ldd --version 2>&1 | grep -qi musl; }; then
  variant="${variant}-musl"
fi
if [ "$arch" = "x64" ] && ! grep -qwi avx2 /proc/cpuinfo 2>/dev/null; then
  variant="${variant}-baseline"
fi

file="opencode-linux-$arch$variant.tar.gz"
url="https://github.com/$REPO/releases/download/v$VER/$file"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

say "версия $VER, файл $file"
command -v curl >/dev/null 2>&1 || { echo "нет curl" >&2; exit 1; }
command -v tar  >/dev/null 2>&1 || { echo "нет tar" >&2; exit 1; }

code="$(curl -sIL -o /dev/null -w '%{http_code}' --max-time 60 "$url" || echo 000)"
if [ "$code" = "404" ]; then
  echo "   ОШИБКА: релиза v$VER для $file нет (HTTP 404)." >&2
  echo "   Доступные: https://github.com/$REPO/releases" >&2
  exit 1
fi
say "HTTP $code, скачиваю (таймаут ${TIMEOUT}s)"

ok=0
for attempt in 1 2 3; do
  say "попытка $attempt/3"
  rm -f "$tmp/$file"
  if ! timeout "$TIMEOUT" curl -sSL --retry 3 --retry-all-errors --retry-delay 3 \
        -o "$tmp/$file" "$url"; then
    say "загрузка не удалась"
  fi
  if [ -s "$tmp/$file" ] && gzip -t "$tmp/$file" 2>/dev/null; then
    say "gzip OK, $(du -h "$tmp/$file" | cut -f1), распаковываю"
    if tar -xzf "$tmp/$file" -C "$tmp" 2>/dev/null && [ -f "$tmp/opencode" ]; then
      ok=1
      break
    fi
    say "распаковка не удалась"
  else
    say "архив битый или неполный ($(stat -c%s "$tmp/$file" 2>/dev/null || echo 0) байт)"
  fi
done

[ "$ok" = "1" ] || { echo "ИТОГ: не удалось получить валидный архив за 3 попытки" >&2; exit 1; }

mkdir -p "$DEST"
mv -f "$tmp/opencode" "$DEST/opencode"
chmod 755 "$DEST/opencode"
say "установлено: $("$DEST/opencode" --version 2>/dev/null || echo 'бинарь не отвечает')"
