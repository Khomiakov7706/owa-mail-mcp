"""The connection test goes the way exchangelib will: through the same proxy, and with legacy TLS
only where the user allowed it."""

import socket
import ssl
import threading
from pathlib import Path

import pytest

from owa_mail_mcp.config import Settings
from owa_mail_mcp.ews_backend import EWSBackend
from owa_mail_mcp.tls import LegacyTLSRequired, ServerUnreachable, probe

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def clean_proxy_env(monkeypatch):
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)


def tls_server(legacy: bool = False):
    """A TLS server that accepts connections and does nothing else. `legacy`: TLS 1.0 only."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(FIXTURES / "ews-cert.pem", FIXTURES / "ews-key.pem")
    if legacy:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1
    srv = socket.create_server(("127.0.0.1", 0))

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            try:
                with ctx.wrap_socket(conn, server_side=True) as tls:
                    tls.recv(1)
            except (OSError, ssl.SSLError):
                pass

    threading.Thread(target=serve, daemon=True).start()
    return srv


def connect_proxy(allow: bool):
    """A minimal HTTP CONNECT proxy; with allow=False it answers 403."""
    srv = socket.create_server(("127.0.0.1", 0))
    seen = []

    def pipe(a, b):
        try:
            while data := a.recv(65536):
                b.sendall(data)
        except OSError:
            pass
        finally:
            for s in (a, b):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def serve():
        while True:
            try:
                client, _ = srv.accept()
            except OSError:
                return
            head = b""
            while b"\r\n\r\n" not in head:
                head += client.recv(4096)
            target = head.split(b" ")[1].decode()
            seen.append(target)
            if not allow:
                client.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                client.close()
                continue
            host, port = target.rsplit(":", 1)
            upstream = socket.create_connection((host, int(port)))
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
            threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    return srv, seen


def test_direct_probe_reads_the_certificate():
    srv = tls_server()
    info = probe("localhost", srv.getsockname()[1])
    assert not info.trusted_by_system and info.proxy is None and "Test Exchange" in info.subject


def test_probe_goes_through_the_proxy_requests_would_use(monkeypatch):
    srv = tls_server()
    proxy, seen = connect_proxy(allow=True)
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{proxy.getsockname()[1]}")
    info = probe("localhost", srv.getsockname()[1])
    assert seen and set(seen) == {f"localhost:{srv.getsockname()[1]}"}
    assert info.proxy == f"http://127.0.0.1:{proxy.getsockname()[1]}"


def test_refusing_proxy_is_named(monkeypatch):
    proxy, _ = connect_proxy(allow=False)
    monkeypatch.setenv("HTTPS_PROXY", f"http://user:pw@127.0.0.1:{proxy.getsockname()[1]}")
    with pytest.raises(ServerUnreachable) as exc:
        probe("localhost", 443)
    assert exc.value.kind == "proxy" and "403" in exc.value.reason
    assert "pw" not in (exc.value.proxy or "")


def test_dns_failure_is_named():
    with pytest.raises(ServerUnreachable) as exc:
        probe("no-such-host.invalid", 443, timeout=5)
    assert exc.value.kind == "dns"


def test_closed_port_is_a_connect_failure():
    sock = socket.create_server(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(ServerUnreachable) as exc:
        probe("127.0.0.1", port, timeout=5)
    assert exc.value.kind == "connect"


def _legacy_supported() -> bool:
    try:
        srv = tls_server(legacy=True)
        probe("localhost", srv.getsockname()[1], legacy_ok=True)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _legacy_supported(), reason="this OpenSSL build cannot speak TLS 1.0 at all")
def test_legacy_tls_is_detected_and_allowed_per_host():
    srv = tls_server(legacy=True)
    port = srv.getsockname()[1]
    with pytest.raises(LegacyTLSRequired):
        probe("localhost", port)
    assert probe("localhost", port, legacy_ok=True).fingerprint


@pytest.mark.skipif(not _legacy_supported(), reason="this OpenSSL build cannot speak TLS 1.0 at all")
def test_exchange_on_legacy_tls_works_only_where_allowed():
    import hashlib
    from http.server import ThreadingHTTPServer

    from test_ews_backend import PASSWORD, FakeExchange

    from owa_mail_mcp.backend import BackendError

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeExchange)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(FIXTURES / "ews-cert.pem", FIXTURES / "ews-key.pem")
    ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    fp = hashlib.sha256(ssl.PEM_cert_to_DER_cert((FIXTURES / "ews-cert.pem").read_text())).hexdigest()
    base = dict(server=f"https://localhost:{port}/EWS/Exchange.asmx", email="ivan@corp.test", username="CORP\\ivan",
                password=PASSWORD, auth_type="basic", trusted_certificates={f"localhost:{port}": fp})
    try:
        with pytest.raises(BackendError):
            EWSBackend(Settings(**base)).whoami()
        me = EWSBackend(Settings(**base, legacy_tls=(f"localhost:{port}",))).whoami()
        assert me.email == "ivan@corp.test"
    finally:
        httpd.shutdown()
