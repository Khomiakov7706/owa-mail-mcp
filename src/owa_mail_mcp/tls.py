"""Server certificates: look at one before trusting it, and pin the one the user trusted.

A corporate Exchange often has a certificate from the company's own CA,
which the system does not trust. Instead of turning verification off, the user sees the
certificate (who issued it, until when, its SHA-256 fingerprint) and trusts exactly that one.
A changed certificate is shown with the old and new fingerprints, so a renewal can be told
from an interception.
"""

from __future__ import annotations

import hashlib
import socket
import ssl
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from requests.adapters import HTTPAdapter


def host_port(server: str) -> tuple[str, int]:
    raw = server.strip()
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    return (parsed.hostname or "").lower(), parsed.port or 443


def pin_key(host: str, port: int) -> str:
    return f"{host.lower()}:{port}"


def format_fingerprint(hex_digest: str) -> str:
    h = hex_digest.replace(":", "").upper()
    return ":".join(h[i : i + 2] for i in range(0, len(h), 2))


@dataclass
class CertificateInfo:
    host: str
    port: int
    fingerprint: str
    """SHA-256, lowercase hex without separators."""
    trusted_by_system: bool
    system_error: str | None = None
    subject: str | None = None
    issuer: str | None = None
    not_after: str | None = None
    names: list[str] | None = None
    proxy: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "key": pin_key(self.host, self.port),
            "fingerprint": self.fingerprint,
            "fingerprint_display": format_fingerprint(self.fingerprint),
            "trusted_by_system": self.trusted_by_system,
            "system_error": self.system_error,
            "subject": self.subject,
            "issuer": self.issuer,
            "not_after": self.not_after,
            "names": self.names or [],
            "proxy": self.proxy,
        }


class ServerUnreachable(Exception):
    """`kind`: dns, connect, proxy, tls. `reason`: what the system said."""

    def __init__(self, message: str, kind: str = "connect", reason: str = "", proxy: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.reason = reason
        self.proxy = proxy


class LegacyTLSRequired(Exception):
    """The server only completes a TLS handshake with settings OpenSSL 3 refuses by default
    (legacy renegotiation, weak DH keys, old ciphers). Browsers often still accept them."""

    def __init__(self, host: str, port: int, reason: str) -> None:
        super().__init__(f"{host}:{port} needs legacy TLS: {reason}")
        self.host, self.port, self.reason = host, port, reason


def apply_transport_settings(settings) -> None:  # noqa: ANN001
    """Pins, legacy-TLS hosts and the proxy, for every requests session made after this."""
    import os

    PinningAdapter.pins = dict(settings.trusted_certificates)
    PinningAdapter.legacy = set(settings.legacy_tls)
    if settings.proxy:
        url = settings.proxy if "://" in settings.proxy else "http://" + settings.proxy
        os.environ["HTTPS_PROXY"] = os.environ["https_proxy"] = url


def proxy_for(host: str) -> str | None:
    """The proxy requests would use for https://host: environment variables and, on macOS and
    Windows, the system proxy settings. A proxy auto-config (PAC) script is not evaluated."""
    import urllib.request

    try:
        if urllib.request.proxy_bypass(host):
            return None
        proxies = urllib.request.getproxies()
    except Exception:
        return None
    return proxies.get("https") or proxies.get("all") or None


def _redact(url: str) -> str:
    p = urlparse(url if "://" in url else "http://" + url)
    return f"{p.scheme}://{p.hostname}:{p.port or 8080}"


def _open(host: str, port: int, timeout: float) -> tuple[socket.socket, str | None]:
    proxy = proxy_for(host)
    if not proxy:
        try:
            return socket.create_connection((host, port), timeout=timeout), None
        except socket.gaierror as exc:
            raise ServerUnreachable(f"{host}: DNS", "dns", str(exc)) from exc
        except OSError as exc:
            raise ServerUnreachable(f"{host}:{port}: TCP", "connect", f"{type(exc).__name__}: {exc}") from exc
    p = urlparse(proxy if "://" in proxy else "http://" + proxy)
    shown = _redact(proxy)
    try:
        sock = socket.create_connection((p.hostname, p.port or 8080), timeout=timeout)
    except OSError as exc:
        raise ServerUnreachable(f"proxy {shown}", "proxy", f"{type(exc).__name__}: {exc}", shown) from exc
    request = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
    if p.username:
        import base64
        from urllib.parse import unquote

        token = base64.b64encode(f"{unquote(p.username)}:{unquote(p.password or '')}".encode()).decode()
        request += f"Proxy-Authorization: Basic {token}\r\n"
    try:
        sock.sendall((request + "\r\n").encode())
        reply = b""
        while b"\r\n\r\n" not in reply and len(reply) < 16384:
            chunk = sock.recv(4096)
            if not chunk:
                break
            reply += chunk
    except OSError as exc:
        sock.close()
        raise ServerUnreachable(f"proxy {shown}", "proxy", f"{type(exc).__name__}: {exc}", shown) from exc
    status = reply.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    if " 200" not in status:
        sock.close()
        raise ServerUnreachable(f"proxy {shown}", "proxy", f"CONNECT {host}:{port} → {status or 'no answer'}", shown)
    return sock, shown


def _context(*, verify: bool, legacy: bool = False) -> ssl.SSLContext:
    if verify:
        # The same trust store requests uses, so "trusted" here means trusted there.
        try:
            import certifi

            ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ctx = ssl.create_default_context()
    else:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if legacy:
        allow_legacy_tls(ctx)
    return ctx


def allow_legacy_tls(ctx: ssl.SSLContext) -> None:
    """What browsers still accept and OpenSSL 3 refuses: legacy renegotiation, small DH keys,
    older ciphers and TLS 1.0/1.1. The certificate is still verified (or pinned)."""
    ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    except ssl.SSLError:
        pass
    try:
        import warnings

        with warnings.catch_warnings():  # deliberate, for this one server only
            warnings.simplefilter("ignore", DeprecationWarning)
            ctx.minimum_version = ssl.TLSVersion.TLSv1
    except (ValueError, AttributeError):
        pass


def _handshake(host: str, port: int, timeout: float, ctx: ssl.SSLContext) -> tuple[bytes | None, str | None]:
    sock, proxy = _open(host, port, timeout)
    try:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            return tls.getpeercert(binary_form=True), proxy
    finally:
        sock.close()


def probe(host: str, port: int = 443, timeout: float = 10.0, legacy_ok: bool = False) -> CertificateInfo:
    """Connects the way requests will (through the same proxy), takes the certificate whether or
    not it is trusted, and says whether it is trusted. Raises ServerUnreachable with the exact
    stage that failed, or LegacyTLSRequired."""
    try:
        der, proxy = _handshake(host, port, timeout, _context(verify=False, legacy=legacy_ok))
    except ssl.SSLError as exc:
        reason = getattr(exc, "reason", None) or str(exc)
        if not legacy_ok:
            try:
                _handshake(host, port, timeout, _context(verify=False, legacy=True))
            except (OSError, ssl.SSLError, ServerUnreachable):
                pass
            else:
                raise LegacyTLSRequired(host, port, str(reason)) from exc
        raise ServerUnreachable(f"{host}:{port}: TLS", "tls", str(reason)) from exc
    except ServerUnreachable:
        raise
    except OSError as exc:
        raise ServerUnreachable(f"{host}:{port}: TLS", "tls", f"{type(exc).__name__}: {exc}") from exc
    if not der:
        raise ServerUnreachable(f"{host}:{port} sent no certificate", "tls", "no certificate")

    trusted, error = True, None
    try:
        _handshake(host, port, timeout, _context(verify=True, legacy=legacy_ok))
    except ssl.SSLCertVerificationError as exc:
        trusted, error = False, exc.verify_message or str(exc)
    except (OSError, ssl.SSLError, ServerUnreachable) as exc:
        trusted, error = False, str(exc)

    info = CertificateInfo(host=host, port=port, fingerprint=hashlib.sha256(der).hexdigest(), trusted_by_system=trusted, system_error=error)
    info.proxy = proxy
    _describe(info, der)
    return info


def _describe(info: CertificateInfo, der: bytes) -> None:
    """Subject, issuer, expiry and names, through the ssl module's own decoder (no extra dependency)."""
    decode = getattr(getattr(ssl, "_ssl", None), "_test_decode_cert", None)
    if decode is None:
        return
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as fh:
        fh.write(ssl.DER_cert_to_PEM_cert(der))
        path = fh.name
    try:
        decoded = decode(path)
    except Exception:
        return
    finally:
        Path(path).unlink(missing_ok=True)

    def name(rdns) -> str:
        parts = [f"{k}={v}" for rdn in rdns or () for k, v in rdn]
        return ", ".join(parts)

    info.subject = name(decoded.get("subject"))
    info.issuer = name(decoded.get("issuer"))
    info.not_after = decoded.get("notAfter")
    info.names = [v for k, v in decoded.get("subjectAltName", ()) if k == "DNS"]


class PinningAdapter(HTTPAdapter):
    """For a pinned host, the certificate must have exactly the trusted fingerprint; the system
    CA store is not consulted. Other hosts are verified as usual."""

    pins: dict[str, str] = {}
    legacy: set[str] = set()
    """`host:port` the user allowed to use legacy TLS settings."""

    def build_connection_pool_key_attributes(self, request, verify, cert=None):  # noqa: ANN001
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        key = pin_key(host_params.get("host") or "", host_params.get("port") or 443)
        pin = self.pins.get(key)
        if host_params.get("scheme") != "https":
            return host_params, pool_kwargs
        if pin:
            for name in ("ca_certs", "ca_cert_dir"):
                pool_kwargs.pop(name, None)
            # The chain is not checked against the CA store; urllib3 checks the fingerprint instead.
            pool_kwargs["ssl_context"] = _unverified_context(legacy=key in self.legacy)
            pool_kwargs["cert_reqs"] = "CERT_NONE"
            pool_kwargs["assert_fingerprint"] = pin
        elif key in self.legacy:
            pool_kwargs["ssl_context"] = _context(verify=True, legacy=True)
        return host_params, pool_kwargs

    def cert_verify(self, conn, url, verify, cert):  # noqa: ANN001
        # requests sets CERT_REQUIRED and the CA bundle on the pool here, after the pool was chosen.
        super().cert_verify(conn, url, verify, cert)
        host, port = host_port(url)
        pin = self.pins.get(pin_key(host, port))
        if pin and url.lower().startswith("https"):
            conn.cert_reqs = "CERT_NONE"
            conn.ca_certs = None
            conn.ca_cert_dir = None
            conn.assert_fingerprint = pin


_UNVERIFIED: dict[bool, ssl.SSLContext] = {}


def _unverified_context(legacy: bool = False) -> ssl.SSLContext:
    if legacy not in _UNVERIFIED:
        _UNVERIFIED[legacy] = _context(verify=False, legacy=legacy)
    return _UNVERIFIED[legacy]
