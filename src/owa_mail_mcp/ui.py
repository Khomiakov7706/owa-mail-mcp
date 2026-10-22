"""The confirmation window for `send_email`.

Only the user's browser gets the address (the server opens it); the agent never sees it. An agent
that also has unrestricted shell access on the same machine as the same user is out of scope: it
could read the password just as well.
"""

from __future__ import annotations

import sys
import threading
from typing import Any

from .confirm import ConfirmationBroker
from .models import Outgoing
from .safety import clean_addresses
from .i18n import current_language, render_page, t
from .web import LocalWebApp


class ConfirmationUI:
    def __init__(self, broker: ConfirmationBroker, host: str = "127.0.0.1", port: int = 0, open_browser: bool = True) -> None:
        self.broker = broker
        self._open_browser = open_browser
        self._lock = threading.Lock()
        self._app = LocalWebApp(lambda: render_page("confirm_page.html", current_language()), host=host, port=port)
        self._app.get("api/pending", lambda: (200, {"items": broker.snapshot()}))
        self._app.post("api/decide", self._decide)

    @property
    def url(self) -> str:
        return self._app.url

    @property
    def port(self) -> int:
        return self._app.port

    @property
    def token(self) -> str:
        return self._app.token

    def start(self) -> None:
        self._app.start()

    def stop(self) -> None:
        self._app.stop()

    def present(self) -> bool:
        """Brings the window up for a new question. False when it could not be opened."""
        with self._lock:
            if self.broker.ui_seen_within(4):
                return True  # already open: it polls every second and shows the new card
            if not self._open_browser:
                print(f"owa-mail-mcp: {t('cli.confirm_open')}: {self.url}", file=sys.stderr, flush=True)
                return True
            return self._app.open_in_browser(t("cli.confirm_open"))

    def _decide(self, data: dict[str, Any]) -> tuple[int, Any]:
        proposal_id = str(data["id"])
        action = data["action"]
        if action not in {"approve", "reject"}:
            return 400, {"error": "bad action"}
        edited = None
        if action == "approve" and isinstance(data.get("edits"), dict) and _is_email(self.broker, proposal_id):
            try:
                edited = _edited_message(self.broker, proposal_id, data["edits"])
            except KeyError:
                return 404, {"error": "This message is no longer waiting.", "code": "gone"}
        action_edits = data["edits"] if action == "approve" and isinstance(data.get("edits"), dict) and edited is None else None
        error = self.broker.decide(proposal_id, action == "approve", edited, action_edits)
        if error:
            return 409, {"error": error, "code": getattr(error, "code", ""), "params": getattr(error, "params", {})}
        return 200, {"ok": True}


def _is_email(broker: ConfirmationBroker, proposal_id: str) -> bool:
    try:
        return broker.get(proposal_id).message is not None
    except KeyError:
        return False


def _edited_message(broker: ConfirmationBroker, proposal_id: str, edits: dict[str, Any]) -> Outgoing:
    current = broker.get(proposal_id).message

    def addresses(key: str, fallback: list[str]) -> list[str]:
        value = edits.get(key)
        if value is None:
            return list(fallback)
        return clean_addresses([value] if isinstance(value, str) else [str(v) for v in value])

    return Outgoing(
        to=addresses("to", current.to),
        cc=addresses("cc", current.cc),
        bcc=addresses("bcc", current.bcc),
        subject=str(edits.get("subject", current.subject)),
        body=str(edits.get("body", current.body)),
        reply_to_id=current.reply_to_id,
    )
