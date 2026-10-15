"""Current settings and backend, and the call journal.

The MCP server keeps running while the user changes settings in the setup window: the config file
is re-read when it changes, and the backend is rebuilt when the connection settings (server,
login, password, trusted certificates) change. A new password also lifts the lock that a rejected
one put on Exchange calls.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable

from .backend import BackendError
from .config import Settings, config_dir, config_path


class NotConfigured(BackendError):
    pass


class UnconfiguredBackend:
    name = "unconfigured"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def __getattr__(self, item: str):
        def fail(*args: Any, **kwargs: Any):
            raise NotConfigured(self.reason)

        return fail


def default_factory(settings: Settings):
    from .__main__ import make_backend

    if not settings.is_configured:
        raise NotConfigured("OWA Mail MCP is not connected to a mailbox yet.")
    return make_backend(settings)


class Runtime:
    def __init__(self, loader: Callable[[], Settings] = Settings.load, factory: Callable[[Settings], Any] = default_factory) -> None:
        self._loader = loader
        self._factory = factory
        self._lock = threading.Lock()
        self._stamp: object = object()
        self._settings: Settings | None = None
        self._backend: Any = None
        self._key: tuple | None = None
        self._fixed = False
        self.generation = 0

    @classmethod
    def fixed(cls, settings: Settings, backend: Any) -> "Runtime":
        rt = cls()
        rt._settings, rt._backend, rt._fixed = settings, backend, True
        return rt

    def current(self) -> tuple[Settings, Any]:
        with self._lock:
            if self._fixed:
                return self._settings, self._backend  # type: ignore[return-value]
            stamp = _file_stamp()
            if stamp != self._stamp or self._settings is None:
                self._stamp = stamp
                settings = self._loader()
                self._settings = settings
                if settings.connection_key() != self._key or self._backend is None:
                    self._key = settings.connection_key()
                    try:
                        self._backend = self._factory(settings)
                    except BackendError as exc:
                        self._backend = UnconfiguredBackend(str(exc))
                    self.generation += 1
            return self._settings, self._backend

    @property
    def settings(self) -> Settings:
        return self.current()[0]

    @property
    def backend(self):
        return self.current()[1]


def _file_stamp() -> object:
    try:
        st = config_path().stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


# --- updates while running -------------------------------------------------------------------
# An MCP client starts the server once and keeps it; reinstalling replaces the files on disk but
# not the code already loaded. Noticing that turns a baffling old bug into "restart the server".

def _code_stamp() -> float:
    package = os.path.dirname(os.path.abspath(__file__))
    try:
        return max(os.path.getmtime(os.path.join(package, f)) for f in os.listdir(package) if f.endswith((".py", ".html")))
    except (OSError, ValueError):
        return 0.0


_STARTED_WITH = _code_stamp()


def code_updated_since_start() -> bool:
    return _code_stamp() > _STARTED_WITH + 1


UPDATED_HINT = (
    " Note: OWA Mail MCP was updated after this server process started, so it is still running the old code. "
    "Ask the user to restart the MCP server in their client (Claude Code: /mcp → owa-mail → Reconnect; "
    "Claude Desktop or Cursor: quit and reopen the app), then try again."
)


# --- journal ------------------------------------------------------------------------------------
# What the assistant called and when, without arguments or results: no mail content on disk.

_JOURNAL_LIMIT = 300
_journal_lock = threading.Lock()


def journal_path():
    return config_dir() / "journal.jsonl"


def record_call(tool: str, client: str | None, is_error: bool) -> None:
    entry = {"time": time.time(), "tool": tool[:64], "client": (client or "")[:80], "error": bool(is_error), "pid": os.getpid()}
    path = journal_path()
    with _journal_lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            if path.stat().st_size > 200_000:
                lines = path.read_text(encoding="utf-8").splitlines()[-_JOURNAL_LIMIT:]
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass


def read_journal(limit: int = 30) -> list[dict[str, Any]]:
    try:
        lines = journal_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out
