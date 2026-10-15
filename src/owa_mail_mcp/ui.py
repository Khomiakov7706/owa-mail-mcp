"""The confirmation window: a page served on 127.0.0.1 under a random secret path.

Only the user's browser gets the address (the server opens it); the agent never sees it. The page
cannot be framed, takes JSON only, and checks Host/Origin, so another web page or a DNS-rebinding
trick cannot press the button. An agent that also has unrestricted shell access on the same
machine as the same user is out of scope: it could read the password just as well.
"""

from __future__ import annotations

import json
import logging
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .confirm import ConfirmationBroker
from .models import Outgoing
from .safety import clean_addresses

log = logging.getLogger(__name__)

_PAGE = (Path(__file__).parent / "confirm_page.html").read_text(encoding="utf-8")


class ConfirmationUI:
    def __init__(self, broker: ConfirmationBroker, host: str = "127.0.0.1", port: int = 0, open_browser: bool = True) -> None:
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("The confirmation window only listens on the loopback interface.")
        self.broker = broker
        self.token = secrets.token_urlsafe(24)
        self._open_browser = open_browser
        self._server = ThreadingHTTPServer((host, port), self._handler())
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._host = host
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        host = "[::1]" if self._host == "::1" else self._host
        return f"http://{host}:{self.port}/t/{self.token}/"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, name="owa-mail-mcp-ui", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def present(self) -> bool:
        """Brings the window up for a new question. False when it could not be opened."""
        with self._lock:
            if self.broker.ui_seen_within(4):
                return True  # already open: it polls every second and shows the new card
            if not self._open_browser:
                print(f"owa-mail-mcp: confirm the message at {self.url}", file=sys.stderr, flush=True)
                return True
            try:
                opened = webbrowser.open(self.url, new=1)
            except Exception:
                opened = False
            if not opened:
                print(f"owa-mail-mcp: open {self.url} to confirm the message", file=sys.stderr, flush=True)
            return opened

    # --- HTTP --------------------------------------------------------------------------------

    def _handler(self):
        ui = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "owa-mail-mcp"
            sys_version = ""

            def log_message(self, fmt: str, *args: Any) -> None:  # keep stdout for MCP only
                pass

            def _allowed_host(self) -> bool:
                host = (self.headers.get("Host") or "").lower()
                return host in {f"127.0.0.1:{ui.port}", f"localhost:{ui.port}", f"[::1]:{ui.port}"}

            def _route(self) -> str | None:
                prefix = f"/t/{ui.token}/"
                path = self.path.split("?", 1)[0]
                if not secrets.compare_digest(path[: len(prefix)].encode(), prefix.encode()):
                    return None
                return path[len(prefix):]

            def _send(self, status: int, body: bytes, content_type: str, nonce: str | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                csp = "default-src 'none'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
                if nonce:
                    csp += f"; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'"
                self.send_header("Content-Security-Policy", csp)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, payload: Any) -> None:
                self._send(status, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

            def do_GET(self) -> None:  # noqa: N802
                route = self._route()
                if not self._allowed_host() or route is None:
                    return self._send(404, b"not found", "text/plain")
                if route == "":
                    nonce = secrets.token_urlsafe(12)
                    page = _PAGE.replace("{{NONCE}}", nonce).encode()
                    return self._send(200, page, "text/html; charset=utf-8", nonce=nonce)
                if route == "api/pending":
                    return self._json(200, {"items": ui.broker.snapshot()})
                return self._send(404, b"not found", "text/plain")

            def do_POST(self) -> None:  # noqa: N802
                route = self._route()
                if not self._allowed_host() or route is None:
                    return self._send(404, b"not found", "text/plain")
                origin = self.headers.get("Origin")
                if origin and origin.lower() not in {f"http://127.0.0.1:{ui.port}", f"http://localhost:{ui.port}", f"http://[::1]:{ui.port}"}:
                    return self._json(403, {"error": "foreign origin"})
                if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                    return self._json(415, {"error": "JSON only"})
                if route != "api/decide":
                    return self._send(404, b"not found", "text/plain")
                try:
                    length = min(int(self.headers.get("Content-Length") or 0), 512_000)
                    data = json.loads(self.rfile.read(length) or b"{}")
                    proposal_id = str(data["id"])
                    action = data["action"]
                except (ValueError, KeyError, TypeError):
                    return self._json(400, {"error": "bad request"})
                if action not in {"approve", "reject"}:
                    return self._json(400, {"error": "bad action"})
                edited = None
                if action == "approve" and isinstance(data.get("edits"), dict):
                    try:
                        edited = _edited_message(ui.broker, proposal_id, data["edits"])
                    except KeyError:
                        return self._json(404, {"error": "This message is no longer waiting."})
                error = ui.broker.decide(proposal_id, action == "approve", edited)
                if error:
                    return self._json(409, {"error": error})
                return self._json(200, {"ok": True})

        return Handler


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
