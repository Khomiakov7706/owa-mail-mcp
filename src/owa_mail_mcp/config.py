"""Settings from environment variables. The password comes from the env or the OS keyring."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

KEYRING_SERVICE = "owa-mail-mcp"


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    backend: str = "ews"
    """`ews` (Exchange Web Services) or `demo` (built-in sample mailbox, no network)."""
    server: str | None = None
    """Host (`mail.company.com`), OWA URL (`https://mail.company.com/owa`) or full EWS URL."""
    email: str | None = None
    username: str | None = None
    """`DOMAIN\\login` or `login@company.com`. Defaults to `email`."""
    password: str | None = field(default=None, repr=False)
    auth_type: str = "ntlm"
    allow_send: bool = False
    """`send_email` is not even listed unless this is on."""
    confirm_timeout: int = 50
    """Seconds the confirmation window waits. Most MCP clients give up on a call after 60."""
    ui_host: str = "127.0.0.1"
    ui_port: int = 0
    """0 picks a free port."""
    open_browser: bool = True
    internal_domains: tuple[str, ...] = ()
    """Domains not flagged as external, besides the user's own."""
    max_recipients: int = 50
    requests_per_minute: int = 120

    @classmethod
    def from_env(cls) -> "Settings":
        email = os.environ.get("OWA_MAIL_EMAIL") or None
        username = os.environ.get("OWA_MAIL_USERNAME") or email
        domains = tuple(d.strip().lower() for d in os.environ.get("OWA_MAIL_INTERNAL_DOMAINS", "").split(",") if d.strip())
        return cls(
            backend=(os.environ.get("OWA_MAIL_BACKEND") or "ews").strip().lower(),
            server=os.environ.get("OWA_MAIL_SERVER") or None,
            email=email,
            username=username,
            password=os.environ.get("OWA_MAIL_PASSWORD") or _keyring_password(username),
            auth_type=(os.environ.get("OWA_MAIL_AUTH") or "ntlm").strip().lower(),
            allow_send=_bool("OWA_MAIL_ALLOW_SEND", False),
            confirm_timeout=_int("OWA_MAIL_CONFIRM_TIMEOUT", 50),
            ui_host=os.environ.get("OWA_MAIL_UI_HOST") or "127.0.0.1",
            ui_port=_int("OWA_MAIL_UI_PORT", 0),
            open_browser=_bool("OWA_MAIL_OPEN_BROWSER", True),
            internal_domains=domains,
            max_recipients=_int("OWA_MAIL_MAX_RECIPIENTS", 50),
            requests_per_minute=_int("OWA_MAIL_REQUESTS_PER_MINUTE", 120),
        )


def _keyring_password(username: str | None) -> str | None:
    if not username:
        return None
    try:
        import keyring
    except ImportError:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, username)
    except Exception:
        return None
