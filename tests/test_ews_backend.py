"""EWSBackend against a tiny fake Exchange over HTTPS: exercises exchangelib for real, including
version negotiation, Basic auth, and certificate pinning."""

import base64
import hashlib
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from owa_mail_mcp.backend import AuthenticationError
from owa_mail_mcp.config import Settings
from owa_mail_mcp.ews_backend import CertificateProblem, EWSBackend

FIXTURES = Path(__file__).parent / "fixtures"
PASSWORD = "secret"

HEADER = (
    '<s:Header><h:ServerVersionInfo MajorVersion="15" MinorVersion="1" MajorBuildNumber="2507" MinorBuildNumber="6" '
    'Version="V2017_07_11" xmlns:h="http://schemas.microsoft.com/exchange/services/2006/types"/></s:Header>'
)
ENVELOPE = (
    '<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    'xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages" '
    'xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types">' + HEADER + "<s:Body>{body}</s:Body></s:Envelope>"
)
CONVERT_ID = (
    '<m:ConvertIdResponse><m:ResponseMessages><m:ConvertIdResponseMessage ResponseClass="Error">'
    "<m:MessageText>Invalid id.</m:MessageText><m:ResponseCode>ErrorInvalidIdMalformed</m:ResponseCode>"
    "<m:DescriptiveLinkKey>0</m:DescriptiveLinkKey></m:ConvertIdResponseMessage></m:ResponseMessages></m:ConvertIdResponse>"
)
RESOLVE_NAMES = (
    '<m:ResolveNamesResponse><m:ResponseMessages><m:ResolveNamesResponseMessage ResponseClass="Success">'
    '<m:ResponseCode>NoError</m:ResponseCode><m:ResolutionSet TotalItemsInView="1" IncludesLastItemInRange="true">'
    "<t:Resolution><t:Mailbox><t:Name>Иван Петров</t:Name><t:EmailAddress>ivan@corp.test</t:EmailAddress>"
    "<t:RoutingType>SMTP</t:RoutingType><t:MailboxType>Mailbox</t:MailboxType></t:Mailbox></t:Resolution>"
    "</m:ResolutionSet></m:ResolveNamesResponseMessage></m:ResponseMessages></m:ResolveNamesResponse>"
)


class FakeExchange(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        expected = "Basic " + base64.b64encode(f"CORP\\ivan:{PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") != expected:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="ews"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if "ConvertId" in body:
            answer = CONVERT_ID
        elif "ResolveNames" in body:
            answer = RESOLVE_NAMES
        else:
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = ENVELOPE.format(body=answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def exchange():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeExchange)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(FIXTURES / "ews-cert.pem", FIXTURES / "ews-key.pem")
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    der = ssl.PEM_cert_to_DER_cert((FIXTURES / "ews-cert.pem").read_text())
    yield server.server_address[1], hashlib.sha256(der).hexdigest()
    server.shutdown()


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


def _settings(port, fingerprint, password=PASSWORD, pin=True):
    return Settings(
        server=f"https://localhost:{port}/EWS/Exchange.asmx", email="ivan@corp.test", username="CORP\\ivan",
        password=password, auth_type="basic",
        trusted_certificates={f"localhost:{port}": fingerprint} if pin else {},
    )


def test_whoami_negotiates_version_first(exchange):
    port, fingerprint = exchange
    me = EWSBackend(_settings(port, fingerprint)).whoami()
    assert me.email == "ivan@corp.test" and me.name == "Иван Петров"


def test_find_people_on_a_fresh_backend(exchange):
    port, fingerprint = exchange
    people = EWSBackend(_settings(port, fingerprint)).find_people("Петров", 5)
    assert people[0].email == "ivan@corp.test"


def test_wrong_password_is_an_authentication_error(exchange):
    port, fingerprint = exchange
    with pytest.raises(AuthenticationError):
        EWSBackend(_settings(port, fingerprint, password="wrong")).whoami()


def test_unpinned_self_signed_certificate_is_refused(exchange):
    port, fingerprint = exchange
    with pytest.raises(CertificateProblem):
        EWSBackend(_settings(port, fingerprint, pin=False)).whoami()
    with pytest.raises(CertificateProblem):
        EWSBackend(_settings(port, "00" * 32)).whoami()
