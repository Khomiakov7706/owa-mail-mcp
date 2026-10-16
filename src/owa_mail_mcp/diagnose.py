"""What the server actually answers on the EWS and OWA addresses, in words a user can act on.

Used when the connection test fails in a way exchangelib cannot explain ("the server answered, but
not like EWS"). Credentials go only to the configured host: redirects to another host are reported,
never followed with them. The report holds status codes, redirect targets, offered sign-in methods,
Exchange headers and page titles: no bodies, no passwords, no cookies.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from .config import Settings
from .i18n import current_language, t
from .tls import PinningAdapter, apply_transport_settings, host_port

_EXCHANGE_HEADERS = ("X-OWA-Version", "X-FEServer", "X-BEServer", "X-CalculatedBETarget", "X-DiagInfo", "request-id", "X-Powered-By")
_SOAP_GET_FOLDER = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
    'xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types" '
    'xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages">'
    '<soap:Header><t:RequestServerVersion Version="Exchange2013"/></soap:Header>'
    "<soap:Body><m:GetFolder><m:FolderShape><t:BaseShape>IdOnly</t:BaseShape></m:FolderShape>"
    '<m:FolderIds><t:DistinguishedFolderId Id="inbox"/></m:FolderIds></m:GetFolder></soap:Body></soap:Envelope>'
)


@dataclass
class Probe:
    label: str
    url: str
    status: int | None = None
    error: str | None = None
    redirects: list[str] = field(default_factory=list)
    stopped_at_foreign_host: str | None = None
    auth_schemes: list[str] = field(default_factory=list)
    content_type: str | None = None
    title: str | None = None
    exchange: dict[str, str] = field(default_factory=dict)
    server: str | None = None
    ews_version: str | None = None
    soap_fault: str | None = None
    lang: str | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], {}) and k != "lang"}

    def line(self) -> str:
        lang = self.lang
        parts = [f"{self.label}: {self.url}"]
        if self.error:
            parts.append(t("diag.error", lang, text=self.error))
        if self.status is not None:
            parts.append(f"HTTP {self.status}")
        if self.redirects:
            parts.append(t("diag.redirects", lang, list=" → ".join(self.redirects)))
        if self.stopped_at_foreign_host:
            parts.append(t("diag.foreign_host", lang, host=self.stopped_at_foreign_host))
        if self.auth_schemes:
            parts.append(t("diag.auth_schemes", lang, list=", ".join(self.auth_schemes)))
        if self.content_type:
            parts.append(t("diag.type", lang, type=self.content_type))
        if self.title:
            parts.append(t("diag.page", lang, title=self.title))
        if self.ews_version:
            parts.append(f"EWS: {self.ews_version}")
        if self.soap_fault:
            parts.append(t("diag.ews_error", lang, text=self.soap_fault))
        if self.exchange:
            parts.append(t("diag.exchange_headers", lang, list=", ".join(f"{k}={v}" for k, v in self.exchange.items())))
        if self.server:
            parts.append(f"Server: {self.server}")
        return "; ".join(parts)


@dataclass
class Diagnosis:
    summary: str
    probes: list[Probe]

    def to_dict(self) -> dict[str, Any]:
        return {"summary": self.summary, "details": [p.line() for p in self.probes], "probes": [p.to_dict() for p in self.probes]}

    def text(self) -> str:
        return self.summary + "\n\n" + "\n".join("- " + p.line() for p in self.probes)


def _session(settings: Settings) -> requests.Session:
    apply_transport_settings(settings)
    session = requests.Session()
    session.mount("https://", PinningAdapter())
    session.headers["User-Agent"] = "owa-mail-mcp diagnose"
    return session


def _auth(settings: Settings):
    login, password = settings.login, settings.password
    if not (login and password):
        return None
    if settings.auth_type == "basic":
        return requests.auth.HTTPBasicAuth(login, password)
    if settings.auth_type == "ntlm":
        from requests_ntlm import HttpNtlmAuth

        return HttpNtlmAuth(login, password)
    return None  # Kerberos and others: probed without credentials


def _probe(session: requests.Session, label: str, url: str, *, method: str = "GET", auth=None, data: str | None = None,
           lang: str | None = None) -> Probe:
    probe = Probe(label=label, url=url, lang=lang)
    home = urlparse(url).hostname
    current = url
    headers = {"Content-Type": "text/xml; charset=utf-8"} if data else {}
    for _ in range(6):
        try:
            resp = session.request(method, current, auth=auth if urlparse(current).hostname == home else None,
                                   data=data, headers=headers, allow_redirects=False, timeout=20)
        except requests.exceptions.SSLError as exc:
            probe.error = t("diag.cert_rejected", lang, text=_short(exc))
            return probe
        except requests.RequestException as exc:
            probe.error = _short(exc)
            return probe
        probe.status = resp.status_code
        probe.server = resp.headers.get("Server")
        probe.content_type = (resp.headers.get("Content-Type") or "").split(";")[0] or None
        probe.auth_schemes = sorted({h.split()[0] for h in _www_authenticate(resp) if h.split()})
        probe.exchange = {h: resp.headers[h][:80] for h in _EXCHANGE_HEADERS if h in resp.headers}
        _read_body(probe, resp)
        location = resp.headers.get("Location")
        if resp.status_code in (301, 302, 303, 307, 308) and location:
            target = urljoin(current, location)
            parsed = urlparse(target)
            probe.redirects.append(f"{resp.status_code} {parsed.hostname}{parsed.path}")
            if parsed.hostname != home:
                probe.stopped_at_foreign_host = parsed.hostname
                return probe
            if resp.status_code == 303 or (resp.status_code in (301, 302) and method == "POST"):
                method, data, headers = "GET", None, {}
            current = target
            continue
        return probe
    return probe


def _www_authenticate(resp: requests.Response) -> list[str]:
    raw = resp.raw.headers.getlist("WWW-Authenticate") if hasattr(resp.raw, "headers") and hasattr(resp.raw.headers, "getlist") else []
    if not raw and resp.headers.get("WWW-Authenticate"):
        raw = [resp.headers["WWW-Authenticate"]]
    out: list[str] = []
    for value in raw:
        out.extend(part.strip() for part in re.split(r",\s*(?=[A-Za-z]+(?:\s|$))", value) if part.strip())
    return out


def _read_body(probe: Probe, resp: requests.Response) -> None:
    text = resp.content[:65536].decode("utf-8", errors="replace")
    match = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if match:
        probe.title = " ".join(html.unescape(match.group(1)).split())[:120] or None
    version = re.search(r"ServerVersionInfo[^>]*MajorVersion=\"(\d+)\"[^>]*MinorVersion=\"(\d+)\"[^>]*MajorBuildNumber=\"(\d+)\"", text)
    if version:
        probe.ews_version = "{}.{}.{}".format(*version.groups())
    fault = re.search(r"<(?:\w+:)?(?:faultstring|MessageText)[^>]*>(.*?)</", text, re.S)
    if fault:
        probe.soap_fault = " ".join(html.unescape(fault.group(1)).split())[:160]


def _short(exc: Exception) -> str:
    text = str(exc)
    reason = re.search(r"Caused by (\w+\(.*)", text)
    return (reason.group(1) if reason else text)[:200]


def diagnose(settings: Settings, ews_url: str, lang: str | None = None) -> Diagnosis:
    lang = lang or current_language(settings)
    host, port = host_port(settings.server or "")
    base = f"https://{host}" + (f":{port}" if port != 443 else "")
    session = _session(settings)
    anonymous = _probe(session, t("diag.ews_anonymous", lang), ews_url, lang=lang)
    signed_in = _probe(session, t("diag.ews_signed_in", lang), ews_url, method="POST", auth=_auth(settings), data=_SOAP_GET_FOLDER,
                       lang=lang) if _auth(settings) else None
    owa = _probe(session, "OWA", base + "/owa/", lang=lang)
    warmed = None
    if _auth(settings) and signed_in and signed_in.error:
        # Sign in through OWA first, then EWS on the same (authenticated) connection.
        fresh = _session(settings)
        owa_signin = _probe(fresh, t("diag.owa_signin", lang), base + "/owa/", auth=_auth(settings), lang=lang)
        warmed = _probe(fresh, t("diag.ews_after_owa", lang), ews_url, method="POST", auth=_auth(settings), data=_SOAP_GET_FOLDER, lang=lang)
        probes = [p for p in (anonymous, signed_in, owa, owa_signin, warmed) if p]
    else:
        probes = [p for p in (anonymous, signed_in, owa) if p]
    return Diagnosis(summary=explain(anonymous, signed_in, owa, settings, warmed, lang=lang), probes=probes)


def _looks_like_login(p: Probe) -> bool:
    hay = " ".join([*p.redirects, p.title or "", p.stopped_at_foreign_host or ""]).lower()
    return any(k in hay for k in ("logon.aspx", "/adfs/", "login", "sso", "auth.owa", "signin", "вход", "portal"))


def _owa_alive(p: Probe) -> bool:
    return bool(p.exchange.get("X-OWA-Version") or "/owa/auth" in " ".join(p.redirects).lower() or (p.status in (200, 401) and not p.error))


def explain(anonymous: Probe, signed_in: Probe | None, owa: Probe, settings: Settings, warmed: Probe | None = None,
            lang: str | None = None) -> str:
    lang = lang or current_language(settings)
    ews = signed_in or anonymous
    if warmed is not None and ews.error:
        if warmed.ews_version and warmed.status == 200:
            return t("diag.warmed_ok", lang, version=warmed.ews_version)
        if warmed.ews_version:
            return t("diag.warmed_error", lang, version=warmed.ews_version, error=warmed.soap_fault or f"HTTP {warmed.status}")
        if warmed.status == 401:
            return t("diag.warmed_401", lang)
        if warmed.error:
            return t("diag.warmed_dropped", lang)
    if anonymous.error and owa.error:
        return t("diag.no_answer", lang)
    if signed_in and signed_in.ews_version:
        if signed_in.status == 200:
            return t("diag.works", lang, version=signed_in.ews_version)
        return t("diag.ews_fault", lang, version=signed_in.ews_version, error=signed_in.soap_fault or f"HTTP {signed_in.status}")
    if ews.stopped_at_foreign_host or _looks_like_login(ews):
        where = ews.stopped_at_foreign_host or (ews.redirects[-1] if ews.redirects else None) \
            or (t("diag.where_page", lang, title=ews.title) if ews.title else t("diag.where_login", lang))
        return t("diag.gateway", lang, where=where)
    if ews.status == 404:
        tail = " " + t("diag.owa_alive", lang) if _owa_alive(owa) else ""
        return t("diag.not_found", lang) + tail + " " + t("diag.not_found_tail", lang)
    if ews.status == 403:
        return t("diag.forbidden", lang)
    if ews.status == 401:
        offered = [s.lower() for s in ews.auth_schemes]
        mine = {"ntlm": "ntlm", "basic": "basic", "kerberos": "negotiate"}.get(settings.auth_type, settings.auth_type)
        if signed_in and offered and mine not in offered and not (mine == "ntlm" and "negotiate" in offered):
            return t("diag.wrong_auth", lang, auth=settings.auth_type, offered=", ".join(ews.auth_schemes))
        return t("diag.bad_credentials", lang)
    if ews.status == 200 and (ews.content_type or "").startswith("text/html"):
        return t("diag.html_page", lang, title=ews.title or t("diag.no_title", lang))
    if ews.status and ews.status >= 500:
        return t("diag.server_error", lang, status=ews.status)
    return t("diag.fallback", lang)
