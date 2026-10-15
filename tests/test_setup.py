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
