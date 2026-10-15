#!/bin/sh
# OWA Mail MCP installer for macOS and Linux.
#
#   curl -LsSf https://raw.githubusercontent.com/Khomiakov7706/owa-mail-mcp/main/install.sh | sh
#
# Installs uv (Astral's Python tool manager) if it is missing, then installs owa-mail-mcp into its
# own environment with a Python uv downloads itself: the system Python (3.9 on macOS) is not used
# or changed. Finally opens the setup window. Run it again to update.
set -eu

SOURCE="${OWA_MAIL_MCP_SOURCE:-git+https://github.com/Khomiakov7706/owa-mail-mcp}"
PYTHON="${OWA_MAIL_MCP_PYTHON:-3.12}"

say() { printf '%s\n' "$*" >&2; }

if command -v uv >/dev/null 2>&1; then
    UV="$(command -v uv)"
elif [ -x "$HOME/.local/bin/uv" ]; then
    UV="$HOME/.local/bin/uv"
elif [ -x "$HOME/.cargo/bin/uv" ]; then
    UV="$HOME/.cargo/bin/uv"
else
    say "==> Устанавливаю uv (https://docs.astral.sh/uv/)"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    UV="$HOME/.local/bin/uv"
    [ -x "$UV" ] || { say "uv не установился: см. сообщения выше."; exit 1; }
fi

say "==> Устанавливаю owa-mail-mcp (Python $PYTHON скачается сам, если его нет)"
"$UV" tool install --python "$PYTHON" --reinstall --force "$SOURCE"

BIN_DIR="$("$UV" tool dir --bin)"
EXE="$BIN_DIR/owa-mail-mcp"
[ -x "$EXE" ] || { say "Не нашёл $EXE после установки."; exit 1; }

# So that `owa-mail-mcp` works in new terminals; MCP clients get the absolute path anyway.
"$UV" tool update-shell >/dev/null 2>&1 || true

say ""
say "==> Готово: $EXE"
say "    Открываю окно настройки. Позже его можно открыть командой: owa-mail-mcp setup"
say ""
if [ "${OWA_MAIL_MCP_NO_SETUP:-}" = "1" ]; then
    exit 0
fi
exec "$EXE" setup </dev/null
