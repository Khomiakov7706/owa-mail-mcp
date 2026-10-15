"""Settings: the config file the setup window writes, overridden by environment variables.

The password never goes into the file: it lives in the OS keyring (Keychain, Credential Manager,
Secret Service), or comes from OWA_MAIL_PASSWORD.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

KEYRING_SERVICE = "owa-mail-mcp"
CONNECTION_FIELDS = ("backend", "server", "email", "username", "password", "auth_type", "trusted_certificates")


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
        """The config file, then environment variables on top, then the password from the keyring."""
        values: dict[str, Any] = {}
        path = config_path()
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
            known = {f.name for f in fields(cls)} - {"password"}
            values = {k: v for k, v in raw.items() if k in known}
            if "internal_domains" in values:
                values["internal_domains"] = tuple(values["internal_domains"] or ())
        values.update(_env_overrides())
        settings = cls(**values)
        if with_password and not settings.password and settings.backend == "ews":
            settings = replace(settings, password=get_password(settings.login))
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
        "OWA_MAIL_UI_HOST": "ui_host",
    }
    for var, name in text.items():
        if env.get(var):
            out[name] = env[var].strip() if name != "password" else env[var]
    for var, name in {"OWA_MAIL_ALLOW_SEND": "allow_send", "OWA_MAIL_OPEN_BROWSER": "open_browser"}.items():
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
    for transient in ("ui_host", "ui_port", "open_browser"):
        data.pop(transient, None)
    data["internal_domains"] = list(settings.internal_domains)
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


# --- keyring ------------------------------------------------------------------------------------

class KeyringUnavailable(Exception):
    pass


def get_password(login: str | None) -> str | None:
    if not login:
        return None
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, login)
    except Exception:
        return None


def set_password(login: str, password: str) -> None:
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, login, password)
    except Exception as exc:
        raise KeyringUnavailable(
            f"The system keyring is not available ({type(exc).__name__}). "
            "Set the password in the OWA_MAIL_PASSWORD environment variable of the MCP client instead."
        ) from exc
