"""Settings: the config file the setup window writes, overridden by environment variables.

The password never goes into the file: it lives in the OS keyring (Keychain, Credential Manager,
Secret Service), bound to the server it was entered for, or comes from OWA_MAIL_PASSWORD.

The file is signed with a key kept in the keyring. A file changed behind the setup window's back
(by hand, by a script, by an agent with a shell) loses its permissions and trusted lists: they go back
to the safe defaults until the user sets them again in the window.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import tempfile
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

KEYRING_SERVICE = "owa-mail-mcp"
CONNECTION_FIELDS = ("backend", "server", "email", "username", "password", "auth_type", "trusted_certificates", "legacy_tls", "proxy")


def config_dir() -> Path:
    override = os.environ.get("OWA_MAIL_CONFIG_DIR")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "owa-mail-mcp"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "owa-mail-mcp"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "owa-mail-mcp"


def config_path() -> Path:
    return config_dir() / "config.json"


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
    """`send_email` refuses to work unless this is on."""
    calendar_enabled: bool = True
    """The calendar tools at all: off hides the user's meetings from the assistant."""
    allow_calendar: bool = False
    """create_meeting, cancel_meeting and reschedule_meeting refuse to work unless this is on."""
    calendar_confirm: str = "window"
    """How the user approves meeting changes: `window` (the local confirmation window) or `chat` (the MCP client asks
    them, via MCP elicitation; the window is used when the client cannot)."""
    allow_organize: bool = False
    """Drafts, read/unread, flags, categories, moving to folders and to Deleted Items: reversible, no confirmation window."""
    trusted_recipients: tuple[str, ...] = ()
    """Addresses the user agreed to: an email whose every recipient is here goes out without the confirmation window."""
    trusted_per_hour: int = 20
    """At most this many emails an hour skip the window; beyond it they wait for the user again."""
    confirm_timeout: int = 50
    """Seconds the confirmation window waits. Most MCP clients give up on a call after 60."""
    ui_host: str = "127.0.0.1"
    ui_port: int = 0
    open_browser: bool = True
    internal_domains: tuple[str, ...] = ()
    """Domains not flagged as external, besides the user's own."""
    max_recipients: int = 50
    requests_per_minute: int = 120
    trusted_certificates: dict[str, str] = field(default_factory=dict)
    """`host:port` -> SHA-256 fingerprint of a server certificate the user chose to trust (pinning)."""
    legacy_tls: tuple[str, ...] = ()
    """`host:port` the user allowed to use old TLS settings that browsers accept and OpenSSL 3 refuses."""
    proxy: str | None = None
    """HTTPS proxy for Exchange, when the browser uses one the system settings do not show (a PAC script)."""
    language: str | None = None
    """Interface language of the windows and messages (`ru`, `en`, `zh`, `es`); None follows the system."""
    tampered: bool = False
    """The file was changed outside the setup window: its permissions were reset to the safe defaults. Not saved."""

    @property
    def login(self) -> str | None:
        return self.username or self.email

    @property
    def is_configured(self) -> bool:
        return self.backend == "demo" or bool(self.server and self.email and self.password)

    def connection_key(self) -> tuple:
        return tuple(repr(getattr(self, name)) for name in CONNECTION_FIELDS)

    @classmethod
    def load(cls, *, with_password: bool = True) -> "Settings":
        """The config file (checked against its signature), then environment variables on top, then the password
        the keyring holds for this login on this server."""
        values: dict[str, Any] = {}
        path = config_path()
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
            if not isinstance(raw, dict):
                raw = {}
            known = {f.name for f in fields(cls)} - {"password", "tampered"}
            values = {k: v for k, v in raw.items() if k in known}
            if not _signature_ok(raw):
                log.warning("config.json was changed outside the setup window: permissions reset to the safe defaults")
                values = {**{k: v for k, v in values.items() if k not in _GUARDED}, "tampered": True}
            for name in ("internal_domains", "legacy_tls", "trusted_recipients"):
                if name in values:
                    values[name] = tuple(values[name] or ())
            _migrate_legacy_password(values.get("username") or values.get("email"), values.get("server"))
        values.update(_env_overrides())
        settings = cls(**values)
        if with_password and not settings.password and settings.backend == "ews":
            settings = replace(settings, password=get_password(settings.login, settings.server))
        return settings

    @classmethod
    def from_env(cls) -> "Settings":
        return cls.load()


def _env_overrides() -> dict[str, Any]:
    env = os.environ
    out: dict[str, Any] = {}
    text = {
        "OWA_MAIL_BACKEND": "backend", "OWA_MAIL_SERVER": "server", "OWA_MAIL_EMAIL": "email",
        "OWA_MAIL_USERNAME": "username", "OWA_MAIL_PASSWORD": "password", "OWA_MAIL_AUTH": "auth_type",
        "OWA_MAIL_UI_HOST": "ui_host", "OWA_MAIL_PROXY": "proxy", "OWA_MAIL_LANG": "language", "OWA_MAIL_CALENDAR_CONFIRM": "calendar_confirm",
    }
    for var, name in text.items():
        if env.get(var):
            out[name] = env[var].strip() if name != "password" else env[var]
    for var, name in {"OWA_MAIL_ALLOW_SEND": "allow_send", "OWA_MAIL_ALLOW_CALENDAR": "allow_calendar", "OWA_MAIL_ALLOW_ORGANIZE": "allow_organize", "OWA_MAIL_CALENDAR": "calendar_enabled", "OWA_MAIL_OPEN_BROWSER": "open_browser"}.items():
        raw = env.get(var, "").strip().lower()
        if raw:
            out[name] = raw in {"1", "true", "yes", "on"}
    for var, name in {
        "OWA_MAIL_CONFIRM_TIMEOUT": "confirm_timeout", "OWA_MAIL_UI_PORT": "ui_port",
        "OWA_MAIL_MAX_RECIPIENTS": "max_recipients", "OWA_MAIL_REQUESTS_PER_MINUTE": "requests_per_minute",
    }.items():
        if env.get(var, "").strip():
            out[name] = int(env[var])
    if env.get("OWA_MAIL_INTERNAL_DOMAINS"):
        out["internal_domains"] = tuple(d.strip().lower() for d in env["OWA_MAIL_INTERNAL_DOMAINS"].split(",") if d.strip())
    if "backend" in out:
        out["backend"] = out["backend"].lower()
    if "auth_type" in out:
        out["auth_type"] = out["auth_type"].lower()
    return out


def save_settings(settings: Settings) -> Path:
    """Writes everything except the password, readable by the user only."""
    data = asdict(settings)
    data.pop("password", None)
    for transient in ("ui_host", "ui_port", "open_browser", "tampered"):
        data.pop(transient, None)
    data["internal_domains"] = list(settings.internal_domains)
    data["legacy_tls"] = list(settings.legacy_tls)
    data["trusted_recipients"] = list(settings.trusted_recipients)
    key = _signing_key()
    if key:
        data["signature"] = _sign(data, key)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


# --- signature ----------------------------------------------------------------------------------

_GUARDED = {
    "allow_send", "allow_calendar", "allow_organize", "calendar_enabled", "calendar_confirm", "trusted_recipients",
    "trusted_per_hour", "internal_domains", "max_recipients", "requests_per_minute", "confirm_timeout",
    "trusted_certificates", "legacy_tls", "proxy",
}
"""What a file changed behind the window's back must not decide: these fall back to their safe defaults."""
_KEY_ENTRY = "settings-signing-key"
_key_cache: dict[str, str] = {}


def _signing_key(create: bool = True) -> str | None:
    """A random key kept in the keyring; None where there is no keyring (the file cannot be protected there)."""
    if "key" in _key_cache:
        return _key_cache["key"]
    try:
        import keyring

        key = keyring.get_password(KEYRING_SERVICE, _KEY_ENTRY)
        if not key and create:
            key = secrets.token_hex(32)
            keyring.set_password(KEYRING_SERVICE, _KEY_ENTRY, key)
            _key_cache["new"] = "1"
    except Exception:
        return None
    if key:
        _key_cache["key"] = key
    return key


def _sign(data: dict[str, Any], key: str) -> str:
    body = json.dumps({k: v for k, v in data.items() if k != "signature"}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()


def _signature_ok(raw: dict[str, Any]) -> bool:
    had_key = _signing_key(create=False) is not None or "key" in _key_cache
    key = _signing_key()
    if key is None:
        return True  # no keyring: nothing to check against
    signature = raw.get("signature")
    if not signature:
        # The first run with signing (an update from an unsigned version): trust the file once and sign it.
        if not had_key:
            try:
                save_settings(Settings(**{k: (tuple(v) if k in ("internal_domains", "legacy_tls", "trusted_recipients") else v)
                                          for k, v in raw.items() if k in {f.name for f in fields(Settings)} - {"password", "tampered"}}))
            except (OSError, TypeError):
                pass
            return True
        return False
    return hmac.compare_digest(str(signature), _sign(raw, key))


def server_host(server: str | None) -> str | None:
    """`mail.company.com`, `https://mail.company.com/owa` or an EWS URL -> `mail.company.com:443`."""
    raw = (server or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    try:
        port = parsed.port or 443
    except ValueError:
        return None
    return f"{parsed.hostname.lower()}:{port}" if parsed.hostname else None


# --- keyring ------------------------------------------------------------------------------------

class KeyringUnavailable(Exception):
    pass


def _entry(login: str, server: str | None) -> str | None:
    host = server_host(server)
    return f"{login}@@{host}" if host else None


def get_password(login: str | None, server: str | None) -> str | None:
    """The password saved for this login on this server. A changed server address gets nothing: the user types
    the password again, so a saved one never goes to a host it was not entered for."""
    entry = _entry(login, server) if login else None
    if not entry:
        return None
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, entry)
    except Exception:
        return None


def set_password(login: str, server: str | None, password: str) -> None:
    entry = _entry(login, server)
    if not entry:
        raise KeyringUnavailable("No server address to save the password for.")
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, entry, password)
    except Exception as exc:
        raise KeyringUnavailable(
            f"The system keyring is not available ({type(exc).__name__}). "
            "Set the password in the OWA_MAIL_PASSWORD environment variable of the MCP client instead."
        ) from exc


def _migrate_legacy_password(login: str | None, server: str | None) -> None:
    """Versions before 0.9 kept the password under the login alone. It moves to the server saved in the file then
    (the one it was entered for), once."""
    entry = _entry(login, server) if login else None
    if not entry:
        return
    try:
        import keyring

        legacy = keyring.get_password(KEYRING_SERVICE, login)
        if legacy is None:
            return
        if keyring.get_password(KEYRING_SERVICE, entry) is None:
            keyring.set_password(KEYRING_SERVICE, entry, legacy)
        keyring.delete_password(KEYRING_SERVICE, login)
    except Exception:
        return
