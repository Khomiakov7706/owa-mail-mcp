import hashlib
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from owa_mail_mcp.config import Settings
from owa_mail_mcp.diagnose import diagnose

FIXTURES = Path(__file__).parent / "fixtures"
PASSWORD = "s3cret-Pa55"
MODE = {"value": "gateway"}
SOAP_OK = (
    '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Header>'
    '<h:ServerVersionInfo MajorVersion="15" MinorVersion="2" MajorBuildNumber="1544" MinorBuildNumber="4" '
    'xmlns:h="http://schemas.microsoft.com/exchange/services/2006/types"/></s:Header><s:Body/></s:Envelope>'
)


class Server(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, headers=(), body=b""):
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self):
        if self.command == "POST":
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
        mode, ews = MODE["value"], self.path.lower().startswith("/ews/")
        owa_headers = [("X-OWA-Version", "15.2.1544.4"), ("X-FEServer", "MAIL01")]
        if self.path.startswith("/owa/auth/logon.aspx"):
            return self._send(200, [("Content-Type", "text/html"), *owa_headers], b"<html><title>Outlook</title></html>")
        if mode == "gateway":
            return self._send(302, [("Location", "/owa/auth/logon.aspx?replaceCurrent=1&url=" + self.path), *owa_headers])
        if mode == "foreign":
            return self._send(302, [("Location", "https://adfs.corp.example/adfs/ls/?wa=wsignin1.0")])
        if mode == "missing":
            return self._send(404, [("Content-Type", "text/html")], b"<title>404</title>") if ews else self._send(200, [("Content-Type", "text/html"), *owa_headers], b"<title>Outlook</title>")
        if mode == "portal":
            return self._send(200, [("Content-Type", "text/html")], "<html><title>Check Point Mobile — вход</title></html>".encode())
        if mode == "ntlm_only":
            return self._send(401, [("WWW-Authenticate", "Negotiate"), ("WWW-Authenticate", "NTLM")])
        if mode == "works":
            if self.headers.get("Authorization", "").startswith("Basic "):
                return self._send(200, [("Content-Type", "text/xml")], SOAP_OK.encode())
            return self._send(401, [("WWW-Authenticate", 'Basic realm="x"')])
        return self._send(500)

    do_GET = do_POST = _route


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(FIXTURES / "ews-cert.pem", FIXTURES / "ews-key.pem")
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    fp = hashlib.sha256(ssl.PEM_cert_to_DER_cert((FIXTURES / "ews-cert.pem").read_text())).hexdigest()
    yield srv.server_address[1], fp
    srv.shutdown()


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


def run(server, mode, auth="basic"):
    port, fp = server
    MODE["value"] = mode
    settings = Settings(server=f"localhost:{port}", email="ivan@corp.test", username="CORP\\ivan", password=PASSWORD,
                        auth_type=auth, trusted_certificates={f"localhost:{port}": fp})
    result = diagnose(settings, f"https://localhost:{port}/EWS/Exchange.asmx")
    assert PASSWORD not in result.text()
    return result


@pytest.mark.parametrize("mode,needle", [
    ("gateway", "закрыт шлюзом с веб-входом"),
    ("foreign", "adfs.corp.example"),
    ("missing", "HTTP 404"),
    ("portal", "Check Point Mobile"),
    ("works", "EWS работает (Exchange 15.2.1544)"),
])
def test_explains_what_the_server_does(server, mode, needle):
    assert needle in run(server, mode).summary


def test_offers_other_sign_in_method(server):
    summary = run(server, "ntlm_only", auth="basic").summary
    assert "Negotiate" in summary and "NTLM" in summary and "Способ входа" in summary


def test_foreign_redirect_is_not_followed(server):
    probe = run(server, "foreign").probes[1]
    assert probe.stopped_at_foreign_host == "adfs.corp.example"
    assert "туда пароль не отправлялся" in probe.line()


def test_missing_mentions_owa(server):
    assert "OWA при этом отвечает" in run(server, "missing").summary
