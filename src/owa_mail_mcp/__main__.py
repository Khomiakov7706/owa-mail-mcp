"""Command line.

  owa-mail-mcp            in a terminal: opens the setup window; started by an MCP client: the stdio server
  owa-mail-mcp setup      the setup window
  owa-mail-mcp serve      the stdio MCP server
  owa-mail-mcp check      tests the saved connection
  owa-mail-mcp status     version, where the settings are, whether the setup is complete (exit 0) or not (exit 2)
  owa-mail-mcp diagnose   shows what the server answers on the EWS and OWA addresses
  owa-mail-mcp connect C  adds the server to an MCP client (claude-code, codex, claude-desktop, cursor, opencode, kilo)

For agents installing this on the user's behalf: `status --json`, `connect --list`, and `setup --server/--email`
to prefill the window. The password is only ever typed by the user, in the setup window.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time

from .backend import BackendError, MailBackend
from .config import Settings


def make_backend(settings: Settings) -> MailBackend:
    if settings.backend == "demo":
        from .demo_backend import DemoBackend

        return DemoBackend()
    if settings.backend == "ews":
        from .ews_backend import EWSBackend

        return EWSBackend(settings)
    raise BackendError(f"Unknown OWA_MAIL_BACKEND {settings.backend!r}: use `ews` or `demo`.")


class _SetupLauncher:
    """Opens the setup window from inside the MCP server when the assistant finds the mailbox not
    connected. At most once every 2 minutes."""

    def __init__(self) -> None:
        self._app = None
        self._last = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> bool:
        from .i18n import t
        from .setup import SetupApp

        with self._lock:
            if self._app is None:
                self._app = SetupApp()
                self._app.start()
            if time.monotonic() - self._last < 120:
                return True
            self._last = time.monotonic()
            return self._app.app.open_in_browser(t("cli.setup_open"))

    def stop(self) -> None:
        if self._app is not None:
            self._app.stop()


def serve() -> None:
    from .runtime import Runtime
    from .server import build_server
    from .ui import ConfirmationUI

    runtime = Runtime()
    launcher = _SetupLauncher()
    settings = runtime.settings
    mcp, tools = build_server(runtime, on_not_configured=launcher, journal=True)
    ui = ConfirmationUI(tools.broker, host=settings.ui_host, port=settings.ui_port, open_browser=settings.open_browser)
    ui.start()
    tools.ui = ui
    try:
        mcp.run("stdio")
    finally:
        ui.stop()
        launcher.stop()


def status(as_json: bool = False) -> int:
    from . import __version__
    from .config import config_path
    from .i18n import LANGUAGES, current_language, t

    settings = Settings.load()
    lang = current_language(settings)
    path = config_path()
    if as_json:
        from .setup import installers, server_command

        command, cmd_args = server_command()
        print(json.dumps({
            "version": __version__,
            "configured": settings.is_configured,
            "server": settings.server, "email": settings.email, "login": settings.login,
            "password_saved": bool(settings.password),
            "sending_allowed": settings.allow_send,
            "language": lang,
            "settings_file": str(path),
            "command": [command, *cmd_args],
            "clients": {k: v["available"] for k, v in installers(lang).items()},
        }, ensure_ascii=False, indent=2))
        return 0 if settings.is_configured else 2
    print(t("cli.version", lang, version=__version__))
    print(t("cli.settings", lang, path=path) + ("" if path.exists() else t("cli.not_created", lang)))
    print(t("cli.language", lang, value=LANGUAGES[lang]))
    if settings.backend == "demo":
        print(t("cli.mailbox_demo", lang))
        return 0
    print(t("cli.server", lang, value=settings.server or "-"))
    print(t("cli.email", lang, value=settings.email or "-"))
    print(t("cli.login", lang, value=settings.login or "-"))
    print(t("cli.password", lang, value=t("cli.password_saved" if settings.password else "cli.password_missing", lang)))
    print(t("cli.sending", lang, value=t("cli.sending_on" if settings.allow_send else "cli.sending_off", lang)))
    return 0 if settings.is_configured else 2


def check() -> int:
    from .backend import SearchQuery
    from .i18n import current_language, t

    settings = Settings.load()
    lang = current_language(settings)
    if not settings.is_configured:
        print(t("cli.not_set_up", lang), file=sys.stderr)
        return 1
    try:
        backend = make_backend(settings)
        me = backend.whoami()
        unread = backend.unread_count()
        latest = backend.search(SearchQuery(folder="inbox", limit=3)).messages
    except BackendError as exc:
        print(t("cli.failed", lang, error=exc), file=sys.stderr)
        return 1
    print(t("cli.ok", lang, who=f"{me.name} " if me.name else "", email=me.email, unread=unread))
    for m in latest:
        sender = (m.sender.name or m.sender.email) if m.sender else "?"
        print(f"  {m.received:%Y-%m-%d %H:%M}  {sender}: {m.subject}")
    print(t("cli.check_sending", lang, value=t("cli.check_sending_on" if settings.allow_send else "cli.sending_off", lang)))
    return 0


def connect(target: str | None, list_only: bool = False) -> int:
    from .setup import CLIENTS, install_client, installers

    if list_only or not target:
        for client, info in installers().items():
            print(f"{client:15} {'found' if info['available'] else 'not found'}")
        return 0 if list_only else 1
    result = install_client(target)
    print(result["message"], file=sys.stdout if result["ok"] else sys.stderr)
    return 0 if result["ok"] else (2 if target not in CLIENTS else 1)


def prefill(server: str | None, email: str | None, username: str | None) -> None:
    """Saves what the agent learned from the user (never a password) so the setup window opens filled in."""
    from dataclasses import replace

    from .config import save_settings

    if not (server or email or username):
        return
    settings = Settings.load(with_password=False)
    save_settings(replace(settings, server=server or settings.server, email=email or settings.email,
                          username=username or settings.username))


def diagnose_cmd() -> int:
    from .diagnose import diagnose
    from .ews_backend import ews_endpoint

    from .i18n import t

    settings = Settings.load()
    if not settings.server:
        print(t("cli.not_set_up"), file=sys.stderr)
        return 1
    print(diagnose(settings, ews_endpoint(settings.server)).text())
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="owa-mail-mcp", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=["serve", "setup", "check", "status", "diagnose", "connect"])
    parser.add_argument("target", nargs="?", help="connect: the MCP client to add the server to")
    parser.add_argument("--no-browser", action="store_true", help="setup: print the address instead of opening the browser")
    parser.add_argument("--server", help="setup: prefill the web mail address")
    parser.add_argument("--email", help="setup: prefill the email address")
    parser.add_argument("--username", help="setup: prefill the login (DOMAIN\\login), if it differs from the email")
    parser.add_argument("--json", action="store_true", help="status: machine-readable output")
    parser.add_argument("--list", action="store_true", help="connect: list the clients found on this computer")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    # stdout belongs to the MCP protocol: every log line goes to stderr.
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, stream=sys.stderr, format="owa-mail-mcp %(levelname)s %(name)s: %(message)s")

    command = args.command
    if command is None:
        # Run by hand in a terminal: the user wants to set things up. Started by a client: serve.
        command = "setup" if sys.stdin.isatty() else "serve"
    if command == "setup":
        from .setup import run_setup

        prefill(args.server, args.email, args.username)
        run_setup(open_browser=not args.no_browser)
    elif command == "check":
        sys.exit(check())
    elif command == "status":
        sys.exit(status(as_json=args.json))
    elif command == "connect":
        sys.exit(connect(args.target, list_only=args.list))
    elif command == "diagnose":
        sys.exit(diagnose_cmd())
    else:
        serve()


if __name__ == "__main__":
    main()
