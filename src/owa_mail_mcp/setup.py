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
from .config import KeyringUnavailable, Settings, config_path, get_password, save_settings, set_password
from .runtime import read_journal
from .tls import LegacyTLSRequired, ServerUnreachable, apply_transport_settings, format_fingerprint, host_port, pin_key, probe
from .web import LocalWebApp, load_page

SERVER_NAME = "owa-mail"
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


def snippets(command: str, args: list[str]) -> list[dict[str, str]]:
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
        {"id": "kilo-legacy", "title": "Kilo Code (старые версии, mcp_settings.json)", "kind": "json", "text": json.dumps(
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
        self.app = LocalWebApp(load_page("setup_page.html"), host=host, port=port)
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
        stored = bool(get_password(settings.login)) if settings.login else False
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
            "access": {
                "allow_send": settings.allow_send,
                "internal_domains": ", ".join(settings.internal_domains),
                "confirm_timeout": settings.confirm_timeout,
            },
            "trusted_certificates": [{"key": k, "fingerprint": format_fingerprint(v)} for k, v in settings.trusted_certificates.items()],
            "config_path": str(config_path()),
            "command": [command, *args],
            "snippets": snippets(command, args),
            "installers": self._installers(),
            "journal": read_journal(20),
            "standalone": self._on_done is not None,
        }

    def _installers(self) -> dict[str, dict[str, Any]]:
        return {
            "claude-code": {"available": bool(shutil.which("claude")), "hint": "команда claude не найдена"},
            "codex": {"available": bool(shutil.which("codex")), "hint": "команда codex не найдена"},
            "claude-desktop": {"available": claude_desktop_config().parent.exists(), "hint": "Claude Desktop не установлен"},
            "cursor": {"available": cursor_config().parent.exists(), "hint": "Cursor не установлен"},
            "opencode": {"available": bool(shutil.which("opencode")) or opencode_config().parent.exists(), "hint": "OpenCode не установлен"},
            "kilo": {"available": bool(shutil.which("kilo")) or kilo_config().parent.exists(), "hint": "Kilo Code (новая версия) не найден"},
        }

    # --- account -----------------------------------------------------------------------------

    def _account_from(self, data: dict[str, Any]) -> tuple[Settings, str | None]:
        saved = Settings.load(with_password=False)
        server = str(data.get("server") or "").strip()
        email = str(data.get("email") or "").strip()
        username = str(data.get("username") or "").strip() or None
        auth = str(data.get("auth_type") or "ntlm").strip().lower()
        if auth not in _AUTH_TYPES:
            auth = "ntlm"
        if not server or not email:
            return saved, "Укажите адрес сервера и почту."
        if "@" not in email:
            return saved, "Почта должна выглядеть как name@company.com."
        if server.lower().startswith("http://"):
            return saved, "Нужен https: по http пароль не отправляется."
        proxy = str(data.get("proxy") or "").strip() or None
        if proxy and not re.match(r"^(https?://)?[\w.-]+:\d{1,5}/?$", proxy):
            return saved, "Прокси указывается как host:port, например proxy.company.com:3128."
        settings = replace(saved, backend="ews", server=server, email=email, username=username, auth_type=auth, password=None, proxy=proxy)
        password = str(data.get("password") or "")
        if not password:
            # An empty field means the stored password, but only for the login it was stored for.
            password = get_password(settings.login) or ""
            if not password:
                return settings, "Введите пароль."
        return replace(settings, password=password), None

    def test_connection(self, data: dict[str, Any]) -> tuple[int, Any]:
        settings, error = self._account_from(data)
        if error:
            return 200, {"ok": False, "stage": "input", "error": error}
        host, port = host_port(settings.server or "")
        if not host:
            return 200, {"ok": False, "stage": "input", "error": "Не удалось разобрать адрес сервера."}

        key = pin_key(host, port)
        apply_transport_settings(settings)
        try:
            cert = self._probe(host, port, legacy_ok=key in settings.legacy_tls)
        except LegacyTLSRequired as exc:
            return 200, {"ok": False, "stage": "legacy_tls", "host": host, "port": port, "reason": exc.reason}
        except ServerUnreachable as exc:
            return 200, {"ok": False, "stage": "network", "error": network_message(host, port, exc), "detail": exc.reason}
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
            return 200, {"ok": False, "stage": "auth", "error": "Exchange не принял логин или пароль. Проверьте их; для входа Windows логин пишется как DOMAIN\\login."}
        except BackendError as exc:
            return 200, {"ok": False, "stage": "exchange", "error": str(exc), "diagnosis": self._diagnose(settings)}
        except Exception as exc:  # exchangelib can raise its own types on odd servers
            return 200, {"ok": False, "stage": "exchange", "error": f"{type(exc).__name__}: {exc}", "diagnosis": self._diagnose(settings)}
        return 200, {
            "ok": True,
            "me": me.to_dict(),
            "unread": unread,
            "latest": [
                {"from": (m.sender.name or m.sender.email) if m.sender else "", "subject": m.subject, "received": m.to_dict()["received"]}
                for m in latest
            ],
        }

    def _diagnose(self, settings: Settings) -> dict[str, Any] | None:
        if self._diagnoser is None:
            return None
        try:
            return self._diagnoser(settings).to_dict()
        except Exception as exc:
            return {"summary": f"Диагностика не удалась: {type(exc).__name__}: {exc}", "details": []}

    def trust_certificate(self, data: dict[str, Any]) -> tuple[int, Any]:
        server = str(data.get("server") or "").strip()
        expected = str(data.get("fingerprint") or "").replace(":", "").lower()
        host, port = host_port(server)
        try:
            cert = self._probe(host, port)
        except ServerUnreachable as exc:
            return 200, {"ok": False, "error": str(exc)}
        # Trust exactly what the user looked at: if the server now shows another one, ask again.
        if cert.fingerprint != expected:
            return 200, {"ok": False, "error": "Сервер прислал другой сертификат, чем показан. Проверьте подключение ещё раз.", "certificate": cert.to_dict()}
        with self._lock:
            settings = Settings.load(with_password=False)
            pins = {**settings.trusted_certificates, pin_key(host, port): cert.fingerprint}
            save_settings(replace(settings, trusted_certificates=pins))
        return 200, {"ok": True}

    def allow_legacy_tls(self, data: dict[str, Any]) -> tuple[int, Any]:
        host, port = host_port(str(data.get("server") or ""))
        if not host:
            return 200, {"ok": False, "error": "Не удалось разобрать адрес сервера."}
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
                    set_password(settings.login or "", typed)
                except KeyringUnavailable as exc:
                    return 200, {"ok": False, "error": str(exc)}
            save_settings(replace(settings, password=None))
        return 200, {"ok": True}

    def save_access(self, data: dict[str, Any]) -> tuple[int, Any]:
        domains = tuple(d.strip().lower().lstrip("@") for d in str(data.get("internal_domains") or "").replace(";", ",").split(",") if d.strip())
        try:
            timeout = int(data.get("confirm_timeout") or 50)
        except (TypeError, ValueError):
            timeout = 50
        timeout = max(15, min(timeout, 600))
        with self._lock:
            settings = Settings.load(with_password=False)
            save_settings(replace(settings, allow_send=bool(data.get("allow_send")), internal_domains=domains, confirm_timeout=timeout))
        return 200, {"ok": True}

    # --- clients -----------------------------------------------------------------------------

    def install(self, data: dict[str, Any]) -> tuple[int, Any]:
        target = str(data.get("target") or "")
        command, args = server_command()
        try:
            if target == "claude-code":
                return 200, _run_cli(["claude", "mcp", "add", "--scope", "user", SERVER_NAME, "--", command, *args], "Claude Code")
            if target == "codex":
                return 200, _run_cli(["codex", "mcp", "add", SERVER_NAME, "--", command, *args], "Codex")
            if target == "claude-desktop":
                path = _merge_json(claude_desktop_config(), "mcpServers", _plain_entry(command, args))
                return 200, {"ok": True, "message": f"Записано в {path}. Перезапустите Claude Desktop."}
            if target == "cursor":
                path = _merge_json(cursor_config(), "mcpServers", _plain_entry(command, args))
                return 200, {"ok": True, "message": f"Записано в {path}. Cursor подхватит сервер сам или после перезапуска."}
            if target == "opencode":
                path = _merge_json(opencode_config(), "mcp", opencode_entry(command, args), schema="https://opencode.ai/config.json")
                return 200, {"ok": True, "message": f"Записано в {path}. Перезапустите OpenCode; проверка: opencode mcp list."}
            if target == "kilo":
                path = _merge_json(kilo_config(), "mcp", opencode_entry(command, args))
                return 200, {"ok": True, "message": f"Записано в {path}. Перезапустите Kilo Code (в VS Code: Reload Window)."}
        except (OSError, ValueError) as exc:
            return 200, {"ok": False, "message": str(exc)}
        return 400, {"ok": False, "message": "unknown target"}

    def done(self, data: dict[str, Any]) -> tuple[int, Any]:
        if self._on_done:
            threading.Timer(0.3, self._on_done).start()
        return 200, {"ok": True}


def _ews_backend(settings: Settings):
    from .ews_backend import EWSBackend

    return EWSBackend(settings)


def network_message(host: str, port: int, exc: ServerUnreachable) -> str:
    if exc.kind == "dns":
        return (f"Имя {host} не находится в DNS. Проверьте, что адрес совпадает с адресом веб-почты в браузере. "
                "Если VPN включён, переподключите его: иногда система не сразу подхватывает DNS-серверы VPN.")
    if exc.kind == "proxy":
        return f"Прокси {exc.proxy} не пропустил соединение с {host}:{port}: {exc.reason}. Проверьте адрес прокси в разделе «Дополнительно»."
    if exc.kind == "tls":
        return f"Защищённое соединение с {host}:{port} не установилось: {exc.reason}."
    hint = system_proxy_hint()
    return (f"Не удаётся соединиться с {host}:{port} ({exc.reason}). Если браузер при этом открывает почту, он идёт "
            "другим путём, чаще всего через прокси." + (" " + hint if hint else
            " Посмотрите адрес прокси в настройках сети или спросите ИТ и укажите его в разделе «Дополнительно» → «Прокси»."))


def system_proxy_hint() -> str:
    """On macOS, says when the system uses a proxy auto-config script, which this program does not read."""
    if sys.platform != "darwin":
        return ""
    try:
        out = subprocess.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    pac = re.search(r"ProxyAutoConfigURLString\s*:\s*(\S+)", out)
    if re.search(r"ProxyAutoConfigEnable\s*:\s*1", out):
        where = f" ({pac.group(1)})" if pac else ""
        return (f"В системе включена автоматическая настройка прокси (PAC){where}; её программа не читает. "
                "Узнайте у ИТ адрес прокси для веб-почты (или найдите его в PAC-файле) и укажите в «Дополнительно» → «Прокси».")
    if re.search(r"ProxyAutoDiscoveryEnable\s*:\s*1", out):
        return "В системе включено автообнаружение прокси (WPAD); его программа не использует. Укажите прокси вручную в «Дополнительно» → «Прокси»."
    return ""


def _ews_diagnosis(settings: Settings):
    from .diagnose import diagnose
    from .ews_backend import ews_endpoint

    return diagnose(settings, ews_endpoint(settings.server or ""))


def _run_cli(argv: list[str], title: str) -> dict[str, Any]:
    exe = shutil.which(argv[0])
    if not exe:
        return {"ok": False, "message": f"{title}: команда {argv[0]} не найдена в PATH. Скопируйте команду и выполните её сами."}
    try:
        proc = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "message": f"{title}: {exc}"}
    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode == 0:
        return {"ok": True, "message": f"Добавлено в {title}." + (f" {output[:300]}" if output else "")}
    if "already exists" in output.lower():
        return {"ok": True, "message": f"В {title} сервер {SERVER_NAME} уже подключён."}
    return {"ok": False, "message": f"{title}: {output[:500] or f'код выхода {proc.returncode}'}"}


def _plain_entry(command: str, args: list[str]) -> dict[str, Any]:
    entry: dict[str, Any] = {"command": command}
    if args:
        entry["args"] = args
    return entry


def _merge_json(path: Path, key: str, entry: dict[str, Any], schema: str | None = None) -> Path:
    """Adds the server under `key`, keeping everything else. A file that is not plain JSON (a
    .jsonc with comments, a broken one) is reported and never rewritten: comments would be lost."""
    data: dict[str, Any] = {}
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if text.strip():
            try:
                data = json.loads(text)
            except ValueError as exc:
                raise ValueError(
                    f"{path} не удалось разобрать как JSON (возможно, в нём комментарии). "
                    "Чтобы ничего не потерять, файл не изменён: добавьте строки из окна вручную."
                ) from exc
            if not isinstance(data, dict):
                raise ValueError(f"{path} is not a JSON object")
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
    print(f"OWA Mail MCP setup: {app.url}", file=sys.stderr, flush=True)
    if open_browser:
        app.app.open_in_browser("open this address to set up OWA Mail MCP")
    print("Press Ctrl+C to stop.", file=sys.stderr, flush=True)
    try:
        finished.wait()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
