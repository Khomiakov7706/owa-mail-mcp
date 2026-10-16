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

# Messages in the system language (OWA_MAIL_LANG wins), like the program's own windows.
LANG_CODE="${OWA_MAIL_LANG:-${LC_ALL:-${LC_MESSAGES:-}}}"
if [ -z "$LANG_CODE" ] && [ "$(uname)" = "Darwin" ]; then
    LANG_CODE="$(defaults read -g AppleLanguages 2>/dev/null | sed -n 2p | tr -d ' ",')"
fi
LANG_CODE="${LANG_CODE:-${LANG:-en}}"
case "$LANG_CODE" in
    ru*)
        M_UV="==> Устанавливаю uv (https://docs.astral.sh/uv/)"
        M_UV_FAIL="uv не установился: см. сообщения выше."
        M_INSTALL="==> Устанавливаю owa-mail-mcp (Python %s скачается сам, если его нет)"
        M_NOT_FOUND="Не нашёл %s после установки."
        M_DONE="==> Готово: %s (%s)"
        M_KEPT="    Настройки сохранены, вводить их заново не нужно."
        M_RESTART="    Перезапустите MCP-сервер в клиенте, чтобы он подхватил новую версию:"
        M_RESTART_HOW="      Claude Code: /mcp → owa-mail → Reconnect;  Claude Desktop, Cursor: закрыть и открыть приложение."
        M_SETUP_CMD="    Окно настройки: owa-mail-mcp setup"
        M_OPENING="    Открываю окно настройки. Позже его можно открыть командой: owa-mail-mcp setup" ;;
    zh*)
        M_UV="==> 正在安装 uv（https://docs.astral.sh/uv/）"
        M_UV_FAIL="uv 安装失败：请查看上方信息。"
        M_INSTALL="==> 正在安装 owa-mail-mcp（如未安装 Python %s，将自动下载）"
        M_NOT_FOUND="安装后未找到 %s。"
        M_DONE="==> 完成：%s（%s）"
        M_KEPT="    设置已保留，无需重新填写。"
        M_RESTART="    请在客户端中重启 MCP 服务器，以加载新版本："
        M_RESTART_HOW="      Claude Code：/mcp → owa-mail → Reconnect；Claude Desktop、Cursor：关闭并重新打开应用。"
        M_SETUP_CMD="    设置窗口：owa-mail-mcp setup"
        M_OPENING="    正在打开设置窗口。之后可用此命令再次打开：owa-mail-mcp setup" ;;
    es*)
        M_UV="==> Instalando uv (https://docs.astral.sh/uv/)"
        M_UV_FAIL="uv no se instaló: revisa los mensajes de arriba."
        M_INSTALL="==> Instalando owa-mail-mcp (Python %s se descarga solo si falta)"
        M_NOT_FOUND="No se encontró %s después de la instalación."
        M_DONE="==> Listo: %s (%s)"
        M_KEPT="    Tu configuración se conserva; no hace falta volver a introducirla."
        M_RESTART="    Reinicia el servidor MCP en tu cliente para que use la nueva versión:"
        M_RESTART_HOW="      Claude Code: /mcp → owa-mail → Reconnect;  Claude Desktop, Cursor: cierra y vuelve a abrir la aplicación."
        M_SETUP_CMD="    Ventana de configuración: owa-mail-mcp setup"
        M_OPENING="    Abriendo la ventana de configuración. Más tarde puedes abrirla con: owa-mail-mcp setup" ;;
    *)
        M_UV="==> Installing uv (https://docs.astral.sh/uv/)"
        M_UV_FAIL="uv was not installed: see the messages above."
        M_INSTALL="==> Installing owa-mail-mcp (Python %s is downloaded if missing)"
        M_NOT_FOUND="Could not find %s after installing."
        M_DONE="==> Done: %s (%s)"
        M_KEPT="    Your settings are kept; no need to enter them again."
        M_RESTART="    Restart the MCP server in your client so it picks up the new version:"
        M_RESTART_HOW="      Claude Code: /mcp → owa-mail → Reconnect;  Claude Desktop, Cursor: quit and reopen the app."
        M_SETUP_CMD="    Setup window: owa-mail-mcp setup"
        M_OPENING="    Opening the setup window. Later you can open it with: owa-mail-mcp setup" ;;
esac

if command -v uv >/dev/null 2>&1; then
    UV="$(command -v uv)"
elif [ -x "$HOME/.local/bin/uv" ]; then
    UV="$HOME/.local/bin/uv"
elif [ -x "$HOME/.cargo/bin/uv" ]; then
    UV="$HOME/.cargo/bin/uv"
else
    say "$M_UV"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    UV="$HOME/.local/bin/uv"
    [ -x "$UV" ] || { say "$M_UV_FAIL"; exit 1; }
fi

say "$(printf "$M_INSTALL" "$PYTHON")"
"$UV" tool install --python "$PYTHON" --reinstall --force "$SOURCE"

BIN_DIR="$("$UV" tool dir --bin)"
EXE="$BIN_DIR/owa-mail-mcp"
[ -x "$EXE" ] || { say "$(printf "$M_NOT_FOUND" "$EXE")"; exit 1; }

# So that `owa-mail-mcp` works in new terminals; MCP clients get the absolute path anyway.
"$UV" tool update-shell >/dev/null 2>&1 || true

say ""
VERSION="$("$EXE" status 2>/dev/null | head -n 1 || true)"
say "$(printf "$M_DONE" "${VERSION:-owa-mail-mcp}" "$EXE")"

# Settings live outside the program (config.json and the system keyring), so an update keeps
# them. Open the setup window only when there is nothing to keep yet.
if "$EXE" status >/dev/null 2>&1; then
    say "$M_KEPT"
    say "$M_RESTART"
    say "$M_RESTART_HOW"
    say "$M_SETUP_CMD"
    exit 0
fi
if [ "${OWA_MAIL_MCP_NO_SETUP:-}" = "1" ]; then
    exit 0
fi
say "$M_OPENING"
say ""
exec "$EXE" setup </dev/null
