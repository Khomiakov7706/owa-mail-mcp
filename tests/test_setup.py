import json
import urllib.request

import pytest
from mcp import Client

from owa_mail_mcp import config, setup as setup_mod
from owa_mail_mcp.backend import AuthenticationError
from owa_mail_mcp.config import Settings
from owa_mail_mcp.demo_backend import DemoBackend
from owa_mail_mcp.runtime import Runtime, UnconfiguredBackend, read_journal
from owa_mail_mcp.server import build_server
from owa_mail_mcp.setup import SetupApp
from owa_mail_mcp.tls import CertificateInfo, ServerUnreachable

FP = "ab" * 32
FP2 = "cd" * 32


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("OWA_MAIL_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for var in ("OWA_MAIL_BACKEND", "OWA_MAIL_SERVER", "OWA_MAIL_EMAIL", "OWA_MAIL_PASSWORD", "OWA_MAIL_ALLOW_SEND"):
        monkeypatch.delenv(var, raising=False)
    vault = {}
    monkeypatch.setattr(config, "get_password", lambda login: vault.get(login))
    monkeypatch.setattr(setup_mod, "get_password", lambda login: vault.get(login))
    monkeypatch.setattr(setup_mod, "set_password", lambda login, pw: vault.__setitem__(login, pw))
    return vault


class Prober:
    def __init__(self, fingerprint=FP, trusted=True, down=False):
        self.fingerprint, self.trusted, self.down = fingerprint, trusted, down

    def __call__(self, host, port, legacy_ok=False):
        if self.down:
            raise ServerUnreachable(f"{host}:{port}", "connect", "ConnectionRefusedError: [Errno 61] Connection refused")
        return CertificateInfo(host=host, port=port, fingerprint=self.fingerprint, trusted_by_system=self.trusted,
                               system_error=None if self.trusted else "self-signed certificate", subject="commonName=mail.corp.ru")


FORM = {"server": "mail.corp.ru", "email": "ivan.petrov@example.com", "username": "CORP\\ipetrov", "password": "secret"}


def test_happy_path_saves_without_password_in_file(isolated):
    app = SetupApp(backend_factory=lambda s: DemoBackend(), prober=Prober())
    assert app.state()["configured"] is False
    _, r = app.test_connection(FORM)
    assert r["ok"] and r["me"]["email"] == "ivan.petrov@example.com" and r["unread"] == 4 and len(r["latest"]) == 3
    _, r = app.save_account(FORM)
    assert r["ok"]
    saved = json.loads(config.config_path().read_text())
    assert saved["server"] == "mail.corp.ru" and saved["username"] == "CORP\\ipetrov" and "password" not in saved
    assert isolated["CORP\\ipetrov"] == "secret"
    state = app.state()
    assert state["configured"] and state["password_stored"]
    # An empty password field now means the stored one.
    _, r = app.test_connection({**FORM, "password": ""})
    assert r["ok"]


def test_untrusted_certificate_is_shown_then_pinned():
    prober = Prober(trusted=False)
    app = SetupApp(backend_factory=lambda s: DemoBackend(), prober=prober)
    _, r = app.test_connection(FORM)
    assert r["stage"] == "certificate" and r["previous_fingerprint"] is None
    assert r["certificate"]["fingerprint_display"].startswith("AB:AB:")
    _, t = app.trust_certificate({"server": "mail.corp.ru", "fingerprint": FP2})
    assert not t["ok"]  # not the certificate the user looked at
    _, t = app.trust_certificate({"server": "mail.corp.ru", "fingerprint": r["certificate"]["fingerprint_display"]})
    assert t["ok"]
    assert Settings.load(with_password=False).trusted_certificates == {"mail.corp.ru:443": FP}
    _, r = app.test_connection(FORM)
    assert r["ok"]
    prober.fingerprint = FP2  # the server now shows another certificate
    _, r = app.test_connection(FORM)
    assert r["stage"] == "certificate" and r["previous_fingerprint"].startswith("AB:AB")
    app.forget_certificate({"key": "mail.corp.ru:443"})
    assert Settings.load(with_password=False).trusted_certificates == {}


def test_errors_are_classified():
    _, r = SetupApp(prober=Prober(down=True)).test_connection(FORM)
    assert r["stage"] == "network" and "Connection refused" in r["error"] and "прокси" in r["error"]

    class Rejecting(DemoBackend):
        def whoami(self):
            raise AuthenticationError("no")

    _, r = SetupApp(backend_factory=lambda s: Rejecting(), prober=Prober()).test_connection(FORM)
    assert r["stage"] == "auth"
    _, r = SetupApp(prober=Prober()).test_connection({**FORM, "server": "http://mail.corp.ru"})
    assert r["stage"] == "input" and "https" in r["error"]
    _, r = SetupApp(prober=Prober()).test_connection({**FORM, "password": ""})
    assert r["stage"] == "input"


def test_access_settings():
    app = SetupApp(prober=Prober())
    app.save_access({"allow_send": True, "internal_domains": "@corp.ru; corp.com", "confirm_timeout": 5})
    s = Settings.load(with_password=False)
    assert s.allow_send and s.internal_domains == ("corp.ru", "corp.com") and s.confirm_timeout == 15


def test_install_into_claude_desktop_keeps_other_servers(tmp_path, monkeypatch):
    path = tmp_path / "home" / "Claude" / "claude_desktop_config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
    monkeypatch.setattr(setup_mod, "claude_desktop_config", lambda: path)
    _, r = SetupApp().install({"target": "claude-desktop"})
    assert r["ok"]
    data = json.loads(path.read_text())
    assert set(data["mcpServers"]) == {"other", "owa-mail"} and data["theme"] == "dark"
    assert path.with_suffix(".json.bak").exists()
    path.write_text("{broken")
    _, r = SetupApp().install({"target": "claude-desktop"})
    assert not r["ok"] and path.read_text() == "{broken"


def test_snippets_use_absolute_command():
    snippets = {s["id"]: s for s in setup_mod.snippets("/opt/bin/owa-mail-mcp", [])}
    assert snippets["claude-code"]["text"] == "claude mcp add --scope user owa-mail -- /opt/bin/owa-mail-mcp"
    assert json.loads(snippets["claude-desktop"]["text"]) == {"mcpServers": {"owa-mail": {"command": "/opt/bin/owa-mail-mcp"}}}
    assert json.loads(snippets["vscode"]["text"])["servers"]["owa-mail"]["type"] == "stdio"


def test_setup_page_is_served_only_under_the_token():
    app = SetupApp(prober=Prober())
    app.start()
    try:
        with urllib.request.urlopen(app.url, timeout=5) as resp:
            assert "Настройка OWA Mail MCP" in resp.read().decode()
        with urllib.request.urlopen(app.url + "api/state", timeout=5) as resp:
            assert json.loads(resp.read())["configured"] is False
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{app.app.port}/t/nope/api/state", timeout=5)
    finally:
        app.stop()


def test_runtime_picks_up_saved_settings():
    rt = Runtime()
    assert isinstance(rt.backend, UnconfiguredBackend)
    config.save_settings(Settings(backend="demo", allow_send=True))
    assert isinstance(rt.backend, DemoBackend) and rt.settings.allow_send
    generation = rt.generation
    config.save_settings(Settings(backend="demo", allow_send=False, confirm_timeout=30))
    assert not rt.settings.allow_send and rt.generation == generation  # same connection, same backend


@pytest.mark.anyio
async def test_unconfigured_server_opens_setup_and_journals_calls():
    opened = []
    mcp, _ = build_server(Runtime(), on_not_configured=lambda: opened.append(1) or True, journal=True)
    async with Client(mcp) as c:
        r = await c.call_tool("check_mail", {})
        assert r.is_error and "setup window has just opened" in r.content[0].text
        config.save_settings(Settings(backend="demo"))
        r = await c.call_tool("check_mail", {})
        assert not r.is_error and r.structured_content["count"] == 4
    assert opened == [1]
    journal = read_journal()
    assert [e["tool"] for e in journal] == ["check_mail", "check_mail"]
    assert [e["error"] for e in journal] == [False, True]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_status_tells_the_installer_whether_to_open_setup(isolated, capsys, monkeypatch):
    from owa_mail_mcp.__main__ import status

    monkeypatch.setenv("OWA_MAIL_LANG", "en")

    assert status() == 2
    config.save_settings(Settings(server="owa.corp.ru", email="ivan@corp.ru", username="CORP\\ivan"))
    assert status() == 2  # no password yet
    isolated["CORP\\ivan"] = "secret"
    assert status() == 0
    out = capsys.readouterr().out
    assert "password: saved" in out and "secret" not in out


def test_opencode_and_kilo_snippets_use_their_own_format():
    snippets = {s["id"]: json.loads(s["text"]) for s in setup_mod.snippets("/Users/ivan/.local/bin/owa-mail-mcp", []) if s["kind"] == "json"}
    assert snippets["opencode"]["mcp"]["owa-mail"] == {"type": "local", "command": ["/Users/ivan/.local/bin/owa-mail-mcp"], "enabled": True}
    assert snippets["opencode"]["$schema"] == "https://opencode.ai/config.json"
    assert snippets["kilo"]["mcp"]["owa-mail"]["type"] == "local"
    assert snippets["kilo-legacy"]["mcpServers"]["owa-mail"]["disabled"] is False


def test_install_into_opencode_keeps_the_rest(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(setup_mod, "server_command", lambda: ("/opt/bin/owa-mail-mcp", []))
    path = tmp_path / "xdg" / "opencode" / "opencode.json"
    _, r = SetupApp().install({"target": "opencode"})  # no file yet: created with $schema
    assert r["ok"], r
    assert json.loads(path.read_text()) == {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {"owa-mail": {"type": "local", "command": ["/opt/bin/owa-mail-mcp"], "enabled": True}},
    }
    path.write_text(json.dumps({"model": "x/y", "mcp": {"other": {"type": "remote", "url": "https://e.x"}}}))
    _, r = SetupApp().install({"target": "opencode"})
    data = json.loads(path.read_text())
    assert data["model"] == "x/y" and set(data["mcp"]) == {"other", "owa-mail"}


def test_jsonc_with_comments_is_never_rewritten(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    path = tmp_path / "xdg" / "kilo" / "kilo.jsonc"
    path.parent.mkdir(parents=True)
    original = '{\n  // my settings\n  "mcp": {}\n}\n'
    path.write_text(original)
    _, r = SetupApp().install({"target": "kilo"})
    assert not r["ok"] and "комментарии" in r["message"]
    assert path.read_text() == original
    path.write_text('{"mcp": {}}')
    _, r = SetupApp().install({"target": "kilo"})
    assert r["ok"] and json.loads(path.read_text())["mcp"]["owa-mail"]["type"] == "local"


def test_agent_commands_status_json_connect_and_prefill(isolated, capsys, monkeypatch, tmp_path):
    from owa_mail_mcp.__main__ import connect, prefill, status

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert status(as_json=True) == 2
    state = json.loads(capsys.readouterr().out)
    assert state["configured"] is False and state["password_saved"] is False and "opencode" in state["clients"]

    # What the agent learned from the user goes into the window, never a password.
    prefill("owa.corp.ru", "ivan@corp.ru", None)
    saved = json.loads(config.config_path().read_text())
    assert saved["server"] == "owa.corp.ru" and saved["email"] == "ivan@corp.ru" and "password" not in saved

    assert connect("opencode") == 0
    written = json.loads((tmp_path / "xdg" / "opencode" / "opencode.json").read_text())
    assert written["mcp"]["owa-mail"]["type"] == "local"
    assert connect("no-such-client") == 2

    isolated["ivan@corp.ru"] = "secret"
    capsys.readouterr()
    assert status(as_json=True) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["configured"] is True and "secret" not in out
