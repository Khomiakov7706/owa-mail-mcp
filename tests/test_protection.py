"""The saved password stays with its server, and the settings file cannot be edited behind the window's back."""

import json

import keyring
import pytest
from mcp import Client

from owa_mail_mcp import config
from owa_mail_mcp.config import Settings, save_settings
from owa_mail_mcp.demo_backend import DemoBackend
from owa_mail_mcp.tls import CertificateInfo

pytestmark = pytest.mark.anyio


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("OWA_MAIL_CONFIG_DIR", str(tmp_path))
    for var in ("OWA_MAIL_SERVER", "OWA_MAIL_EMAIL", "OWA_MAIL_PASSWORD", "OWA_MAIL_ALLOW_SEND", "OWA_MAIL_BACKEND"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path / "config.json"


def _edit(path, **changes):
    data = json.loads(path.read_text())
    data.update(changes)
    path.write_text(json.dumps(data))


async def test_saved_password_never_goes_to_another_server(cfg):
    from owa_mail_mcp.setup import SetupApp

    used = []
    def factory(settings):
        used.append((settings.server, settings.password))
        return DemoBackend()

    prober = lambda host, port, legacy_ok=False: CertificateInfo(host, port, "ab" * 32, True)  # noqa: E731
    app = SetupApp(backend_factory=factory, prober=prober)
    form = {"server": "mail.corp.ru", "email": "ivan@corp.ru", "username": "CORP\\ivan", "password": "secret"}
    assert app.save_account(form)[1]["ok"]
    assert config.get_password("CORP\\ivan", "https://mail.corp.ru/owa") == "secret"  # same host, other spelling

    _, r = app.test_connection({**form, "password": ""})  # same server: the saved password is used
    assert r["ok"] and used[-1] == ("mail.corp.ru", "secret")

    _, r = app.test_connection({**form, "server": "evil.example.com", "password": ""})
    assert r["ok"] is False and "изменился" in r["error"]
    assert all(server != "evil.example.com" for server, _ in used)
    _, r = app.save_account({**form, "server": "evil.example.com", "password": ""})
    assert r["ok"] is False

    # A running server whose file now names another host has no password to send there.
    _edit(cfg, server="evil.example.com")
    assert Settings.load().password is None
    assert config.get_password("CORP\\ivan", "evil.example.com") is None


def test_an_old_password_moves_to_the_server_it_was_saved_for(cfg):
    save_settings(Settings(server="mail.corp.ru", email="ivan@corp.ru", username="CORP\\ivan"))
    store = keyring.get_keyring()
    store.set_password("owa-mail-mcp", "CORP\\ivan", "old-secret")  # how versions before 0.9 kept it
    assert Settings.load().password == "old-secret"
    assert ("owa-mail-mcp", "CORP\\ivan") not in store.data
    assert config.get_password("CORP\\ivan", "other.corp.ru") is None


async def test_editing_the_file_resets_permissions(cfg):
    from owa_mail_mcp.server import build_server

    save_settings(Settings(backend="demo", server="mail.corp.ru", email="ivan@corp.ru", allow_send=True,
                           trusted_recipients=("anna@corp.ru",), allow_organize=True))
    assert Settings.load().allow_send is True and Settings.load().tampered is False

    _edit(cfg, trusted_recipients=["anna@corp.ru", "attacker@evil.example.com"])
    s = Settings.load()
    assert s.tampered is True and s.allow_send is False and s.allow_organize is False and s.trusted_recipients == ()
    assert s.server == "mail.corp.ru" and s.email == "ivan@corp.ru"  # identity stays; the password is guarded by the host

    mcp, _ = build_server(s, DemoBackend())
    async with Client(mcp) as c:
        status = (await c.call_tool("get_status", {})).structured_content
        r = await c.call_tool("send_email", {"to": ["anna@corp.ru"], "subject": "x", "body": "y"})
    assert status["settings_reset"] is True
    assert r.is_error and "changed outside the setup window" in r.content[0].text

    # Saving from the window signs the file again.
    from owa_mail_mcp.setup import SetupApp

    assert SetupApp().save_access({"allow_send": True, "trusted_recipients": "anna@corp.ru"})[1] == {"ok": True}
    again = Settings.load()
    assert again.tampered is False and again.allow_send is True and again.trusted_recipients == ("anna@corp.ru",)
    assert "signature" in json.loads(cfg.read_text())


def test_an_unsigned_file_from_an_older_version_is_trusted_once(cfg):
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"server": "mail.corp.ru", "email": "ivan@corp.ru", "allow_send": True}))
    first = Settings.load()
    assert first.allow_send is True and first.tampered is False
    assert "signature" in json.loads(cfg.read_text())
    _edit(cfg, allow_send=True, allow_organize=True)
    assert Settings.load().tampered is True
