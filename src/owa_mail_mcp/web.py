"""A small local web app for the windows the user answers in (setup, send confirmation).

Served on the loopback interface under a random secret path that only the user's browser gets.
Pages cannot be framed, take JSON only, and check Host and Origin, so another web page or a
DNS-rebinding trick cannot call them.
"""

from __future__ import annotations

import json
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

Handler = Callable[[dict[str, Any]], tuple[int, Any]]

_ASSETS = Path(__file__).parent


def load_page(name: str) -> str:
    return (_ASSETS / name).read_text(encoding="utf-8")


class LocalWebApp:
    def __init__(self, page: str, host: str = "127.0.0.1", port: int = 0) -> None:
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Local windows only listen on the loopback interface.")
        self.token = secrets.token_urlsafe(24)
        self._page = page
        self._get: dict[str, Callable[[], tuple[int, Any]]] = {}
        self._post: dict[str, Handler] = {}
        self._host = host
        self._server = ThreadingHTTPServer((host, port), self._handler())
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host = "[::1]" if self._host == "::1" else self._host
        return f"http://{host}:{self.port}/t/{self.token}/"

    def get(self, route: str, handler: Callable[[], tuple[int, Any]]) -> None:
        self._get[route] = handler

    def post(self, route: str, handler: Handler) -> None:
        self._post[route] = handler

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, name="owa-mail-mcp-web", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def open_in_browser(self, announce: str) -> bool:
        try:
            opened = webbrowser.open(self.url, new=1)
        except Exception:
            opened = False
        if not opened:
            print(f"owa-mail-mcp: {announce}: {self.url}", file=sys.stderr, flush=True)
        return opened

    def _handler(self):
        app = self
        hosts = lambda: {f"127.0.0.1:{app.port}", f"localhost:{app.port}", f"[::1]:{app.port}"}  # noqa: E731

        class Handler(BaseHTTPRequestHandler):
            server_version = "owa-mail-mcp"
            sys_version = ""

            def log_message(self, fmt: str, *args: Any) -> None:  # stdout belongs to MCP
                pass

            def _route(self) -> str | None:
                if (self.headers.get("Host") or "").lower() not in hosts():
                    return None
                prefix = f"/t/{app.token}/"
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
                if route is None:
                    return self._send(404, b"not found", "text/plain")
                if route == "":
                    nonce = secrets.token_urlsafe(12)
                    return self._send(200, app._page.replace("{{NONCE}}", nonce).encode(), "text/html; charset=utf-8", nonce=nonce)
                handler = app._get.get(route)
                if handler is None:
                    return self._send(404, b"not found", "text/plain")
                self._reply(handler)

            def do_POST(self) -> None:  # noqa: N802
                route = self._route()
                if route is None:
                    return self._send(404, b"not found", "text/plain")
                origin = (self.headers.get("Origin") or "").lower()
                if origin and origin not in {f"http://{h}" for h in hosts()}:
                    return self._json(403, {"error": "foreign origin"})
                if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                    return self._json(415, {"error": "JSON only"})
                handler = app._post.get(route)
                if handler is None:
                    return self._send(404, b"not found", "text/plain")
                try:
                    length = min(int(self.headers.get("Content-Length") or 0), 512_000)
                    data = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(data, dict):
                        raise ValueError
                except ValueError:
                    return self._json(400, {"error": "bad request"})
                self._reply(lambda: handler(data))

            def _reply(self, call) -> None:
                # Bad input from the page is a 400; anything else is our bug and says so.
                try:
                    status, payload = call()
                except (KeyError, TypeError, ValueError) as exc:
                    return self._json(400, {"ok": False, "error": f"bad request: {exc}"})
                except Exception as exc:
                    return self._json(500, {"ok": False, "error": f"internal error: {type(exc).__name__}: {exc}"})
                try:
                    body = json.dumps(payload, ensure_ascii=False, default=str).encode()
                except (TypeError, ValueError) as exc:
                    return self._json(500, {"ok": False, "error": f"internal error: {exc}"})
                self._send(status, body, "application/json; charset=utf-8")

        return Handler
