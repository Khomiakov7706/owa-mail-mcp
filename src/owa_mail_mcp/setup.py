"""The setup window: connect the mailbox, choose what the assistant may do, and hook up MCP clients.

An account form with "Test connection" (and a trust prompt for a certificate the system does not
know), the AI access switches, ready-to-paste client configuration, and the call journal.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .backend import AuthenticationError, BackendError, SearchQuery
from .config import KeyringUnavailable, Settings, config_path, get_password, save_settings, server_host, set_password
from .i18n import LANGUAGES, current_language, normalize, render_page, t
from .runtime import read_journal
from .text import is_valid_address
from .tls import LegacyTLSRequired, ServerUnreachable, apply_transport_settings, format_fingerprint, host_port, pin_key, probe
from .web import LocalWebApp

SERVER_NAME = "owa-mail"
CLIENTS = ("claude-code", "codex", "claude-desktop", "cursor", "opencode", "kilo")
_AUTH_TYPES = ("ntlm", "basic", "kerberos")


def server_command() -> tuple[str, list[str]]:
    """How an MCP client should start this server: an absolute path, since GUI clients do not
    see the shell's PATH."""
    argv0 = Path(sys.argv[0])
    if argv0.name.lower().startswith("owa-mail-mcp") and argv0.exists():
        return str(argv0.absolute()), []
    found = shutil.which("owa-mail-mcp")
    if found:
        return str(Path(found).absolute()), []
    return sys.executable, ["-m", "owa_mail_mcp"]


def _shell_quote(arg: str) -> str:
    if arg and all(c.isalnum() or c in "/._-:\\" for c in arg):
        return arg
    return '"' + arg.replace('"', '\\"') + '"'


def snippets(command: str, args: list[str], lang: str | None = None) -> list[dict[str, str]]:
    cmdline = " ".join(_shell_quote(a) for a in [command, *args])
    entry: dict[str, Any] = {"command": command}
    if args:
        entry["args"] = args
    return [
        {"id": "claude-code", "title": "Claude Code", "kind": "shell", "text": f"claude mcp add --scope user {SERVER_NAME} -- {cmdline}"},
        {"id": "codex", "title": "Codex", "kind": "shell", "text": f"codex mcp add {SERVER_NAME} -- {cmdline}"},
        {"id": "claude-desktop", "title": "Claude Desktop", "kind": "json", "text": json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2, ensure_ascii=False)},
        {"id": "cursor", "title": "Cursor", "kind": "json", "text": json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2, ensure_ascii=False)},
        {"id": "vscode", "title": "VS Code", "kind": "json", "text": json.dumps({"servers": {SERVER_NAME: {"type": "stdio", **entry}}}, indent=2, ensure_ascii=False)},
        {"id": "opencode", "title": "OpenCode", "kind": "json", "text": json.dumps(
            {"$schema": "https://opencode.ai/config.json", "mcp": {SERVER_NAME: opencode_entry(command, args)}}, indent=2, ensure_ascii=False)},
        {"id": "kilo", "title": "Kilo Code", "kind": "json", "text": json.dumps({"mcp": {SERVER_NAME: opencode_entry(command, args)}}, indent=2, ensure_ascii=False)},
        {"id": "kilo-legacy", "title": t("msg.kilo_legacy_title", lang), "kind": "json", "text": json.dumps(
            {"mcpServers": {SERVER_NAME: {**entry, "args": list(args), "disabled": False, "alwaysAllow": []}}}, indent=2, ensure_ascii=False)},
    ]


def opencode_entry(command: str, args: list[str]) -> dict[str, Any]:
    """OpenCode (and Kilo Code, built on it): key `mcp`, a local server with the command as a list."""
    return {"type": "local", "command": [command, *args], "enabled": True}


def claude_desktop_config() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform == "win32":
        import os

        return Path(os.environ.get("APPDATA") or Path.home()) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def cursor_config() -> Path:
    return Path.home() / ".cursor" / "mcp.json"


def _xdg_config() -> Path:
    import os

    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def opencode_config() -> Path:
    """`~/.config/opencode/opencode.json` (a `.jsonc` next to it wins if that is what exists)."""
    base = _xdg_config() / "opencode"
    jsonc = base / "opencode.jsonc"
    return jsonc if jsonc.exists() and not (base / "opencode.json").exists() else base / "opencode.json"


def kilo_config() -> Path:
    """`~/.config/kilo/kilo.jsonc` (or `kilo.json` if that is what exists)."""
    base = _xdg_config() / "kilo"
    plain = base / "kilo.json"
    return plain if plain.exists() and not (base / "kilo.jsonc").exists() else base / "kilo.jsonc"


class SetupApp:
    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        on_done: Callable[[], None] | None = None,
        backend_factory: Callable[[Settings], Any] | None = None,
        prober: Callable[[str, int], Any] = probe,
        diagnoser: Callable[[Settings], Any] | None = None,
    ) -> None:
        self._on_done = on_done
        self._factory = backend_factory or _ews_backend
        self._probe = prober
        self._diagnoser = diagnoser if diagnoser is not None else (_ews_diagnosis if backend_factory is None else None)
        self._lock = threading.Lock()
        self.app = LocalWebApp(lambda: render_page("setup_page.html", current_language()), host=host, port=port)
        self.app.get("api/state", lambda: (200, self.state()))
        self.app.get("api/journal", lambda: (200, {"journal": read_journal(20)}))
        self.app.post("api/test", self.test_connection)
        self.app.post("api/trust", self.trust_certificate)
        self.app.post("api/forget-certificate", self.forget_certificate)
        self.app.post("api/allow-legacy-tls", self.allow_legacy_tls)
        self.app.post("api/save-account", self.save_account)
        self.app.post("api/access", self.save_access)
        self.app.post("api/install", self.install)
        self.app.post("api/done", self.done)
        self.app.post("api/language", self.save_language)

    @property
    def url(self) -> str:
        return self.app.url

    def start(self) -> None:
        self.app.start()

    def stop(self) -> None:
        self.app.stop()

    # --- state -------------------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        settings = Settings.load(with_password=False)
        lang = current_language(settings)
        stored = bool(get_password(settings.login, settings.server)) if settings.login else False
        command, args = server_command()
        return {
            "version": __version__,
            "configured": bool(settings.server and settings.email and (stored or settings.password)),
            "account": {
                "server": settings.server or "",
                "email": settings.email or "",
                "username": settings.username if settings.username and settings.username != settings.email else "",
                "auth_type": settings.auth_type,
                "proxy": settings.proxy or "",
            },
            "legacy_tls": list(settings.legacy_tls),
            "password_stored": stored,
            "tampered": settings.tampered,
            "access": {
                "allow_send": settings.allow_send,
                "allow_calendar": settings.allow_calendar,
                "allow_organize": settings.allow_organize,
                "calendar_enabled": settings.calendar_enabled,
                "calendar_confirm": settings.calendar_confirm,
                "trusted_recipients": "\n".join(settings.trusted_recipients),
                "internal_domains": ", ".join(settings.internal_domains),
                "confirm_timeout": settings.confirm_timeout,
            },
            "trusted_certificates": [{"key": k, "fingerprint": format_fingerprint(v)} for k, v in settings.trusted_certificates.items()],
            "config_path": str(config_path()),
            "command": [command, *args],
            "snippets": snippets(command, args, lang),
            "installers": self._installers(lang),
            "language": normalize(settings.language) or "",
            "languages": LANGUAGES,
            "journal": read_journal(20),
            "standalone": self._on_done is not None,
        }

    def _installers(self, lang: str) -> dict[str, dict[str, Any]]:
        return installers(lang)

    # --- account -----------------------------------------------------------------------------

    def _account_from(self, data: dict[str, Any]) -> tuple[Settings, str | None]:
        saved = Settings.load(with_password=False)
        lang = current_language(saved)
        server = str(data.get("server") or "").strip()
        email = str(data.get("email") or "").strip()
        username = str(data.get("username") or "").strip() or None
        auth = str(data.get("auth_type") or "ntlm").strip().lower()
        if auth not in _AUTH_TYPES:
            auth = "ntlm"
        if not server or not email:
            return saved, t("msg.enter_server_email", lang)
        if "@" not in email:
            return saved, t("msg.email_format", lang)
        if server.lower().startswith("http://"):
            return saved, t("msg.https_needed", lang)
        proxy = str(data.get("proxy") or "").strip() or None
        if proxy and not re.match(r"^(https?://)?[\w.-]+:\d{1,5}/?$", proxy):
            return saved, t("msg.proxy_format", lang)
        settings = replace(saved, backend="ews", server=server, email=email, username=username, auth_type=auth, password=None, proxy=proxy)
        password = str(data.get("password") or "")
        if not password:
            # An empty field means the stored password, but only for the login and the server it was entered for:
            # a changed address never gets it.
            password = get_password(settings.login, settings.server) or ""
            if not password:
                moved = bool(get_password(saved.login, saved.server)) and server_host(saved.server) != server_host(settings.server)
                return settings, t("msg.password_new_server" if moved else "msg.enter_password", lang)
        return replace(settings, password=password), None

    def test_connection(self, data: dict[str, Any]) -> tuple[int, Any]:
        settings, error = self._account_from(data)
        lang = current_language(settings)
        if error:
            return 200, {"ok": False, "stage": "input", "error": error}
        host, port = host_port(settings.server or "")
        if not host:
            return 200, {"ok": False, "stage": "input", "error": t("msg.bad_server", lang)}

        key = pin_key(host, port)
        apply_transport_settings(settings)
        try:
            cert = self._probe(host, port, legacy_ok=key in settings.legacy_tls)
        except LegacyTLSRequired as exc:
            return 200, {"ok": False, "stage": "legacy_tls", "host": host, "port": port, "reason": exc.reason}
        except ServerUnreachable as exc:
            return 200, {"ok": False, "stage": "network", "error": network_message(host, port, exc, lang), "detail": exc.reason}
        pinned = settings.trusted_certificates.get(key)
        if pinned and pinned != cert.fingerprint:
            return 200, {"ok": False, "stage": "certificate", "certificate": cert.to_dict(), "previous_fingerprint": format_fingerprint(pinned)}
        if not cert.trusted_by_system and not pinned:
            return 200, {"ok": False, "stage": "certificate", "certificate": cert.to_dict(), "previous_fingerprint": None}

        try:
            backend = self._factory(settings)
            me = backend.whoami()
            unread = backend.unread_count()
            latest = backend.search(SearchQuery(folder="inbox", limit=3)).messages
        except AuthenticationError:
            return 200, {"ok": False, "stage": "auth", "error": t("msg.auth_failed", lang)}
        except BackendError as exc:
            return 200, {"ok": False, "stage": "exchange", "error": str(exc), "diagnosis": self._diagnose(settings, lang)}
        except Exception as exc:  # exchangelib can raise its own types on odd servers
            return 200, {"ok": False, "stage": "exchange", "error": f"{type(exc).__name__}: {exc}", "diagnosis": self._diagnose(settings, lang)}
        return 200, {
            "ok": True,
            "me": me.to_dict(),
            "unread": unread,
            "latest": [
                {"from": (m.sender.name or m.sender.email) if m.sender else "", "subject": m.subject, "received": m.to_dict()["received"]}
                for m in latest
            ],
        }

    def _diagnose(self, settings: Settings, lang: str) -> dict[str, Any] | None:
        if self._diagnoser is None:
            return None
        try:
            return self._diagnoser(settings).to_dict()
        except Exception as exc:
            return {"summary": t("msg.diag_failed", lang, error=f"{type(exc).__name__}: {exc}"), "details": []}

    def trust_certificate(self, data: dict[str, Any]) -> tuple[int, Any]:
        server = str(data.get("server") or "").strip()
        expected = str(data.get("fingerprint") or "").replace(":", "").lower()
        host, port = host_port(server)
        lang = current_language()
        try:
            cert = self._probe(host, port)
        except ServerUnreachable as exc:
            return 200, {"ok": False, "error": network_message(host, port, exc, lang)}
        # Trust exactly what the user looked at: if the server now shows another one, ask again.
        if cert.fingerprint != expected:
            return 200, {"ok": False, "error": t("msg.cert_changed_retry", lang), "certificate": cert.to_dict()}
        with self._lock:
            settings = Settings.load(with_password=False)
            pins = {**settings.trusted_certificates, pin_key(host, port): cert.fingerprint}
            save_settings(replace(settings, trusted_certificates=pins))
        return 200, {"ok": True}

    def allow_legacy_tls(self, data: dict[str, Any]) -> tuple[int, Any]:
        host, port = host_port(str(data.get("server") or ""))
        if not host:
            return 200, {"ok": False, "error": t("msg.bad_server")}
        key = pin_key(host, port)
        with self._lock:
            settings = Settings.load(with_password=False)
            if key not in settings.legacy_tls:
                save_settings(replace(settings, legacy_tls=(*settings.legacy_tls, key)))
        return 200, {"ok": True}

    def forget_certificate(self, data: dict[str, Any]) -> tuple[int, Any]:
        key = str(data.get("key") or "")
        with self._lock:
            settings = Settings.load(with_password=False)
            pins = {k: v for k, v in settings.trusted_certificates.items() if k != key}
            save_settings(replace(settings, trusted_certificates=pins))
        return 200, {"ok": True}

    def save_account(self, data: dict[str, Any]) -> tuple[int, Any]:
        settings, error = self._account_from(data)
        if error:
            return 200, {"ok": False, "error": error}
        typed = str(data.get("password") or "")
        with self._lock:
            if typed:
                try:
                    set_password(settings.login or "", settings.server, typed)
                except KeyringUnavailable as exc:
                    return 200, {"ok": False, "error": t("msg.keyring_unavailable", error=str(exc.__cause__ or exc))}
            save_settings(replace(settings, password=None))
        return 200, {"ok": True}

    def save_access(self, data: dict[str, Any]) -> tuple[int, Any]:
        domains = tuple(d.strip().lower().lstrip("@") for d in str(data.get("internal_domains") or "").replace(";", ",").split(",") if d.strip())
        try:
            timeout = int(data.get("confirm_timeout") or 50)
        except (TypeError, ValueError):
            timeout = 50
        timeout = max(15, min(timeout, 600))
        trusted = parse_addresses(str(data.get("trusted_recipients") or ""))
        bad = [a for a in trusted if not is_valid_address(a)]
        if bad:
            return 200, {"ok": False, "error": t("msg.trusted_invalid", list=", ".join(bad[:5]))}
        with self._lock:
            settings = Settings.load(with_password=False)
            save_settings(replace(settings, allow_send=bool(data.get("allow_send")), allow_calendar=bool(data.get("allow_calendar")),
                                  allow_organize=bool(data.get("allow_organize")), trusted_recipients=tuple(trusted),
                                  calendar_enabled=bool(data.get("calendar_enabled", settings.calendar_enabled)),
                                  calendar_confirm="chat" if data.get("calendar_confirm", settings.calendar_confirm) == "chat" else "window",
                                  internal_domains=domains, confirm_timeout=timeout))
        return 200, {"ok": True}

    def save_language(self, data: dict[str, Any]) -> tuple[int, Any]:
        language = normalize(str(data.get("language") or ""))
        with self._lock:
            settings = Settings.load(with_password=False)
            save_settings(replace(settings, language=language))
        return 200, {"ok": True}

    # --- clients -----------------------------------------------------------------------------

    def install(self, data: dict[str, Any]) -> tuple[int, Any]:
        result = install_client(str(data.get("target") or ""))
        return (200 if "unknown" not in result else 400), {k: v for k, v in result.items() if k != "unknown"}

    def done(self, data: dict[str, Any]) -> tuple[int, Any]:
        if self._on_done:
            threading.Timer(0.3, self._on_done).start()
        return 200, {"ok": True}


def parse_addresses(text: str) -> list[str]:
    """One address per line (commas and semicolons work too), lowercased, without duplicates."""
    out: list[str] = []
    for part in text.replace(";", "\n").replace(",", "\n").split():
        address = part.strip().strip("<>").lower()
        if address and address not in out:
            out.append(address)
    return out


def installers(lang: str | None = None) -> dict[str, dict[str, Any]]:
    """Clients the setup window (and `owa-mail-mcp connect`) can add the server to, and whether each is present."""
    missing = lambda cmd: t("msg.hint_cli_missing", lang, cmd=cmd)  # noqa: E731
    absent = lambda app: t("msg.hint_not_installed", lang, app=app)  # noqa: E731
    return {
        "claude-code": {"available": bool(shutil.which("claude")), "hint": missing("claude")},
        "codex": {"available": bool(shutil.which("codex")), "hint": missing("codex")},
        "claude-desktop": {"available": claude_desktop_config().parent.exists(), "hint": absent("Claude Desktop")},
        "cursor": {"available": cursor_config().parent.exists(), "hint": absent("Cursor")},
        "opencode": {"available": bool(shutil.which("opencode")) or opencode_config().parent.exists(), "hint": absent("OpenCode")},
        "kilo": {"available": bool(shutil.which("kilo")) or kilo_config().parent.exists(), "hint": t("msg.hint_kilo_missing", lang)},
    }


def install_client(target: str, lang: str | None = None) -> dict[str, Any]:
    """Adds the server to one MCP client's configuration: `{"ok": bool, "message": str}`."""
    command, args = server_command()
    lang = lang or current_language()
    try:
        if target == "claude-code":
            return _run_cli(["claude", "mcp", "add", "--scope", "user", SERVER_NAME, "--", command, *args], "Claude Code", lang)
        if target == "codex":
            return _run_cli(["codex", "mcp", "add", SERVER_NAME, "--", command, *args], "Codex", lang)
        if target == "claude-desktop":
            path = _merge_json(claude_desktop_config(), "mcpServers", _plain_entry(command, args), lang=lang)
            return {"ok": True, "message": t("msg.written_claude_desktop", lang, path=path)}
        if target == "cursor":
            path = _merge_json(cursor_config(), "mcpServers", _plain_entry(command, args), lang=lang)
            return {"ok": True, "message": t("msg.written_cursor", lang, path=path)}
        if target == "opencode":
            path = _merge_json(opencode_config(), "mcp", opencode_entry(command, args), schema="https://opencode.ai/config.json", lang=lang)
            return {"ok": True, "message": t("msg.written_opencode", lang, path=path)}
        if target == "kilo":
            path = _merge_json(kilo_config(), "mcp", opencode_entry(command, args), lang=lang)
            return {"ok": True, "message": t("msg.written_kilo", lang, path=path)}
    except (OSError, ValueError) as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": False, "message": f"unknown client {target!r}: use one of {', '.join(CLIENTS)}", "unknown": True}


def _ews_backend(settings: Settings):
    from .ews_backend import EWSBackend

    return EWSBackend(settings)


def network_message(host: str, port: int, exc: ServerUnreachable, lang: str | None = None) -> str:
    if exc.kind == "dns":
        return t("msg.net_dns", lang, host=host)
    if exc.kind == "proxy":
        return t("msg.net_proxy", lang, proxy=exc.proxy, host=host, port=port, reason=exc.reason)
    if exc.kind == "tls":
        return t("msg.net_tls", lang, host=host, port=port, reason=exc.reason)
    hint = system_proxy_hint(lang)
    return t("msg.net_connect", lang, host=host, port=port, reason=exc.reason) + " " + (hint or t("msg.net_proxy_hint", lang))


def system_proxy_hint(lang: str | None = None) -> str:
    """On macOS, says when the system uses a proxy auto-config script, which this program does not read."""
    if sys.platform != "darwin":
        return ""
    try:
        out = subprocess.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    pac = re.search(r"ProxyAutoConfigURLString\s*:\s*(\S+)", out)
    if re.search(r"ProxyAutoConfigEnable\s*:\s*1", out):
        return t("msg.proxy_pac", lang, where=f" ({pac.group(1)})" if pac else "")
    if re.search(r"ProxyAutoDiscoveryEnable\s*:\s*1", out):
        return t("msg.proxy_wpad", lang)
    return ""


def _ews_diagnosis(settings: Settings):
    from .diagnose import diagnose
    from .ews_backend import ews_endpoint

    return diagnose(settings, ews_endpoint(settings.server or ""))


def _run_cli(argv: list[str], title: str, lang: str | None = None) -> dict[str, Any]:
    exe = shutil.which(argv[0])
    if not exe:
        return {"ok": False, "message": t("msg.cli_not_found", lang, title=title, cmd=argv[0])}
    try:
        proc = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "message": f"{title}: {exc}"}
    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode == 0:
        return {"ok": True, "message": t("msg.cli_added", lang, title=title) + (f" {output[:300]}" if output else "")}
    if "already exists" in output.lower():
        return {"ok": True, "message": t("msg.cli_exists", lang, title=title, name=SERVER_NAME)}
    return {"ok": False, "message": f"{title}: {output[:500] or t('msg.cli_exit_code', lang, code=proc.returncode)}"}


def _plain_entry(command: str, args: list[str]) -> dict[str, Any]:
    entry: dict[str, Any] = {"command": command}
    if args:
        entry["args"] = args
    return entry


def _merge_json(path: Path, key: str, entry: dict[str, Any], schema: str | None = None, lang: str | None = None) -> Path:
    """Adds the server under `key`, keeping everything else. A file that is not plain JSON (a
    .jsonc with comments, a broken one) is reported and never rewritten: comments would be lost."""
    data: dict[str, Any] = {}
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if text.strip():
            try:
                data = json.loads(text)
            except ValueError as exc:
                raise ValueError(t("msg.json_unparsable", lang, path=path)) from exc
            if not isinstance(data, dict):
                raise ValueError(t("msg.json_not_object", lang, path=path))
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    elif schema:
        data["$schema"] = schema
    data.setdefault(key, {})[SERVER_NAME] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def run_setup(open_browser: bool = True) -> None:
    """`owa-mail-mcp setup`: serves the window until the user presses Done or Ctrl+C."""
    finished = threading.Event()
    app = SetupApp(on_done=finished.set)
    app.start()
    print(t("cli.setup_url", url=app.url), file=sys.stderr, flush=True)
    if open_browser:
        app.app.open_in_browser(t("cli.setup_open"))
    print(t("cli.ctrl_c"), file=sys.stderr, flush=True)
    try:
        finished.wait()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
