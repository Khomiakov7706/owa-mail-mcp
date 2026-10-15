"""Command line.

  owa-mail-mcp            in a terminal: opens the setup window; started by an MCP client: the stdio server
  owa-mail-mcp setup      the setup window
  owa-mail-mcp serve      the stdio MCP server
  owa-mail-mcp check      tests the saved connection
"""

from __future__ import annotations

import argparse
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
        from .setup import SetupApp

        with self._lock:
            if self._app is None:
                self._app = SetupApp()
                self._app.start()
            if time.monotonic() - self._last < 120:
                return True
            self._last = time.monotonic()
            return self._app.app.open_in_browser("open this address to set up OWA Mail MCP")

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


def check() -> int:
    from .backend import SearchQuery

    settings = Settings.load()
    if not settings.is_configured:
        print("Not set up yet: run `owa-mail-mcp setup`.", file=sys.stderr)
        return 1
    try:
        backend = make_backend(settings)
        me = backend.whoami()
        unread = backend.unread_count()
        latest = backend.search(SearchQuery(folder="inbox", limit=3)).messages
    except BackendError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"OK: {me.name or ''} <{me.email}>, unread in Inbox: {unread}")
    for m in latest:
        sender = (m.sender.name or m.sender.email) if m.sender else "?"
        print(f"  {m.received:%Y-%m-%d %H:%M}  {sender}: {m.subject}")
    print(f"Sending: {'allowed, with the confirmation window' if settings.allow_send else 'off'}")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="owa-mail-mcp", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=["serve", "setup", "check"])
    parser.add_argument("--no-browser", action="store_true", help="setup: print the address instead of opening the browser")
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

        run_setup(open_browser=not args.no_browser)
    elif command == "check":
        sys.exit(check())
    else:
        serve()


if __name__ == "__main__":
    main()
