import keyring
import pytest
from keyring.backend import KeyringBackend

from owa_mail_mcp.config import Settings
from owa_mail_mcp.demo_backend import DemoBackend
from owa_mail_mcp.server import build_server
from owa_mail_mcp.ui import ConfirmationUI


class MemoryKeyring(KeyringBackend):
    """Tests never touch the real keychain."""

    priority = 1

    def __init__(self):
        super().__init__()
        self.data = {}

    def get_password(self, service, username):
        return self.data.get((service, username))

    def set_password(self, service, username, password):
        self.data[(service, username)] = password

    def delete_password(self, service, username):
        self.data.pop((service, username), None)


@pytest.fixture(autouse=True)
def memory_keyring(monkeypatch):
    from owa_mail_mcp import config

    previous = keyring.get_keyring()
    store = MemoryKeyring()
    keyring.set_keyring(store)
    monkeypatch.setattr(config, "_key_cache", {})
    yield store
    keyring.set_keyring(previous)


@pytest.fixture(autouse=True)
def russian_interface(monkeypatch):
    """Most tests check the Russian texts; tests of other languages set OWA_MAIL_LANG themselves."""
    monkeypatch.setenv("OWA_MAIL_LANG", "ru")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def backend():
    return DemoBackend()


@pytest.fixture
def make_server(backend):
    started = []

    def make(**overrides):
        settings = Settings(**{"backend": "demo", "allow_send": True, "confirm_timeout": 5, "open_browser": False, **overrides})
        mcp, tools = build_server(settings, backend)
        if settings.allow_send:
            ui = ConfirmationUI(tools.broker, open_browser=False)
            ui.start()
            tools.ui = ui
            started.append(ui)
        return mcp, tools

    yield make
    for ui in started:
        ui.stop()
