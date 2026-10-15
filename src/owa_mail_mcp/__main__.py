"""Command line: `owa-mail-mcp` runs the stdio MCP server; `check` and `set-password` help set it up."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from .backend import BackendError, MailBackend
from .config import KEYRING_SERVICE, Settings


def make_backend(settings: Settings) -> MailBackend:
    if settings.backend == "demo":
        from .demo_backend import DemoBackend

        return DemoBackend()
    if settings.backend == "ews":
        from .ews_backend import EWSBackend

        return EWSBackend(settings)
    raise BackendError(f"Unknown OWA_MAIL_BACKEND {settings.backend!r}: use `ews` or `demo`.")


def serve(settings: Settings) -> None:
    from .server import build_server
    from .ui import ConfirmationUI

    try:
        backend = make_backend(settings)
    except BackendError as exc:
        print(f"owa-mail-mcp: {exc}", file=sys.stderr)
        sys.exit(1)
    ui = None
    mcp, tools = build_server(settings, backend)
    if settings.allow_send:
        ui = ConfirmationUI(tools.broker, host=settings.ui_host, port=settings.ui_port, open_browser=settings.open_browser)
        ui.start()
        tools.ui = ui
        logging.getLogger("owa_mail_mcp").info("confirmation window on 127.0.0.1:%s", ui.port)
    try:
        mcp.run("stdio")
    finally:
        if ui:
            ui.stop()


def check(settings: Settings) -> int:
    from .backend import SearchQuery

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
        sender = m.sender.name or m.sender.email if m.sender else "?"
        print(f"  {m.received:%Y-%m-%d %H:%M}  {sender}: {m.subject}")
    print(f"Sending {'ENABLED (with confirmation window)' if settings.allow_send else 'disabled (set OWA_MAIL_ALLOW_SEND=1)'}")
    return 0


def set_password(settings: Settings) -> int:
    try:
        import keyring
    except ImportError:
        print("Install the keyring extra: pip install 'owa-mail-mcp[keyring]'", file=sys.stderr)
        return 1
    username = settings.username or input("Username (DOMAIN\\login or email): ").strip()
    password = getpass.getpass(f"Password for {username}: ")
    keyring.set_password(KEYRING_SERVICE, username, password)
    print(f"Saved in the system keyring (service {KEYRING_SERVICE!r}, user {username!r}).")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="owa-mail-mcp", description=__doc__)
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "check", "set-password"])
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    # stdout belongs to the MCP protocol: every log line goes to stderr.
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, stream=sys.stderr, format="owa-mail-mcp %(levelname)s %(name)s: %(message)s")
    logging.getLogger("owa_mail_mcp").setLevel(logging.DEBUG if args.verbose else logging.INFO)
    settings = Settings.from_env()
    if args.command == "check":
        sys.exit(check(settings))
    if args.command == "set-password":
        sys.exit(set_password(settings))
    serve(settings)


if __name__ == "__main__":
    main()
