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
        }


class ServerUnreachable(Exception):
    pass


def probe(host: str, port: int = 443, timeout: float = 8.0) -> CertificateInfo:
    """Connects, takes the certificate whether or not it is trusted, and says whether the system trusts it."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError) as exc:
        raise ServerUnreachable(f"{host}:{port} — {type(exc).__name__}: {exc}") from exc
    if not der:
        raise ServerUnreachable(f"{host}:{port} sent no certificate")

    trusted, error = True, None
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ssl.create_default_context().wrap_socket(sock, server_hostname=host):
                pass
    except ssl.SSLCertVerificationError as exc:
        trusted, error = False, exc.verify_message or str(exc)
    except (OSError, ssl.SSLError) as exc:
        trusted, error = False, str(exc)

    info = CertificateInfo(host=host, port=port, fingerprint=hashlib.sha256(der).hexdigest(), trusted_by_system=trusted, system_error=error)
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

    def build_connection_pool_key_attributes(self, request, verify, cert=None):  # noqa: ANN001
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        pin = self.pins.get(pin_key(host_params.get("host") or "", host_params.get("port") or 443))
        if pin and host_params.get("scheme") == "https":
            for key in ("ca_certs", "ca_cert_dir"):
                pool_kwargs.pop(key, None)
            # The chain is not checked against the CA store; urllib3 checks the fingerprint instead.
            pool_kwargs["ssl_context"] = _unverified_context()
            pool_kwargs["cert_reqs"] = "CERT_NONE"
            pool_kwargs["assert_fingerprint"] = pin
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


_UNVERIFIED: ssl.SSLContext | None = None


def _unverified_context() -> ssl.SSLContext:
    global _UNVERIFIED
    if _UNVERIFIED is None:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        _UNVERIFIED = ctx
    return _UNVERIFIED
