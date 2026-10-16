import pytest

from owa_mail_mcp.config import Settings
from owa_mail_mcp.demo_backend import DemoBackend
from owa_mail_mcp.server import build_server
from owa_mail_mcp.ui import ConfirmationUI


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
