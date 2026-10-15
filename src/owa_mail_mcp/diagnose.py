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
from .tls import PinningAdapter, host_port

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

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], {})}

    def line(self) -> str:
        parts = [f"{self.label}: {self.url}"]
        if self.error:
            parts.append(f"ошибка: {self.error}")
        if self.status is not None:
            parts.append(f"HTTP {self.status}")
        if self.redirects:
            parts.append("перенаправления: " + " → ".join(self.redirects))
        if self.stopped_at_foreign_host:
            parts.append(f"уводит на другой хост {self.stopped_at_foreign_host} (туда пароль не отправлялся)")
        if self.auth_schemes:
            parts.append("способы входа: " + ", ".join(self.auth_schemes))
        if self.content_type:
            parts.append(f"тип: {self.content_type}")
        if self.title:
            parts.append(f"страница: «{self.title}»")
        if self.ews_version:
            parts.append(f"EWS: {self.ews_version}")
        if self.soap_fault:
            parts.append(f"ошибка EWS: {self.soap_fault}")
        if self.exchange:
            parts.append("заголовки Exchange: " + ", ".join(f"{k}={v}" for k, v in self.exchange.items()))
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
    PinningAdapter.pins = dict(settings.trusted_certificates)
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


def _probe(session: requests.Session, label: str, url: str, *, method: str = "GET", auth=None, data: str | None = None) -> Probe:
    probe = Probe(label=label, url=url)
    home = urlparse(url).hostname
    current = url
    headers = {"Content-Type": "text/xml; charset=utf-8"} if data else {}
    for _ in range(6):
        try:
            resp = session.request(method, current, auth=auth if urlparse(current).hostname == home else None,
                                   data=data, headers=headers, allow_redirects=False, timeout=20)
        except requests.exceptions.SSLError as exc:
            probe.error = "сертификат не принят: " + _short(exc)
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


def diagnose(settings: Settings, ews_url: str) -> Diagnosis:
    host, port = host_port(settings.server or "")
    base = f"https://{host}" + (f":{port}" if port != 443 else "")
    session = _session(settings)
    anonymous = _probe(session, "EWS без входа", ews_url)
    signed_in = _probe(session, "EWS со входом", ews_url, method="POST", auth=_auth(settings), data=_SOAP_GET_FOLDER) if _auth(settings) else None
    owa = _probe(session, "OWA", base + "/owa/")
    probes = [p for p in (anonymous, signed_in, owa) if p]
    return Diagnosis(summary=explain(anonymous, signed_in, owa, settings), probes=probes)


def _looks_like_login(p: Probe) -> bool:
    hay = " ".join([*p.redirects, p.title or "", p.stopped_at_foreign_host or ""]).lower()
    return any(k in hay for k in ("logon.aspx", "/adfs/", "login", "sso", "auth.owa", "signin", "вход", "portal"))


def _owa_alive(p: Probe) -> bool:
    return bool(p.exchange.get("X-OWA-Version") or "/owa/auth" in " ".join(p.redirects).lower() or (p.status in (200, 401) and not p.error))


def explain(anonymous: Probe, signed_in: Probe | None, owa: Probe, settings: Settings) -> str:
    ews = signed_in or anonymous
    if anonymous.error and owa.error:
        return "Сервер не отвечает ни на EWS, ни на OWA. Проверьте адрес и VPN."
    if signed_in and signed_in.ews_version:
        if signed_in.status == 200:
            return (f"EWS работает (Exchange {signed_in.ews_version}) и принял вход. Если проверка всё равно не проходит, "
                    "пришлите эту диагностику разработчику.")
        return f"EWS отвечает (Exchange {signed_in.ews_version}), но вернул ошибку: {signed_in.soap_fault or 'HTTP ' + str(signed_in.status)}."
    if ews.stopped_at_foreign_host or _looks_like_login(ews):
        where = ews.stopped_at_foreign_host or (ews.redirects[-1] if ews.redirects else None) \
            or (f"страницу «{ews.title}»" if ews.title else "страницу входа")
        return (f"Адрес EWS закрыт шлюзом с веб-входом: запрос уводит на {where}. Так бывает, когда снаружи компания "
                "публикует только веб-почту (OWA), а EWS доступен лишь из корпоративной сети. Попробуйте с включённым VPN; "
                "если не поможет, спросите ИТ, доступен ли EWS (/EWS/Exchange.asmx) и по какому адресу.")
    if ews.status == 404:
        tail = " Веб-почта OWA при этом отвечает." if _owa_alive(owa) else ""
        return "По адресу /EWS/Exchange.asmx ничего нет (HTTP 404): EWS здесь не опубликован или выключен." + tail + \
            " Спросите ИТ, доступен ли EWS и по какому адресу; его можно указать в поле «Адрес сервера» полностью."
    if ews.status == 403:
        return "Доступ к EWS запрещён (HTTP 403): администраторы закрыли EWS для вашей учётной записи или снаружи сети. Попробуйте с VPN или спросите ИТ."
    if ews.status == 401:
        offered = [s.lower() for s in ews.auth_schemes]
        mine = {"ntlm": "ntlm", "basic": "basic", "kerberos": "negotiate"}.get(settings.auth_type, settings.auth_type)
        if signed_in and offered and mine not in offered and not (mine == "ntlm" and "negotiate" in offered):
            return f"EWS не принимает выбранный способ входа ({settings.auth_type}). Сервер предлагает: {', '.join(ews.auth_schemes)}. Смените «Способ входа» в разделе «Дополнительно»."
        return "EWS есть, но не принял логин и пароль. Проверьте их; для входа Windows логин пишется как DOMAIN\\login."
    if ews.status == 200 and (ews.content_type or "").startswith("text/html"):
        return f"Вместо EWS сервер отдаёт веб-страницу «{ews.title or 'без заголовка'}». Похоже, адрес ведёт на портал или шлюз, а не на Exchange. Проверьте адрес и VPN."
    if ews.status and ews.status >= 500:
        return f"EWS отвечает ошибкой сервера (HTTP {ews.status}). Попробуйте позже или сообщите ИТ."
    return "Сервер отвечает не как EWS. Подробности ниже: пришлите их разработчику или в ИТ."
