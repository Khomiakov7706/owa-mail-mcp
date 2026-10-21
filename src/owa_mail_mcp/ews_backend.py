"""Exchange Web Services through exchangelib: on-prem Exchange 2013/2016/2019 and Exchange Online with Basic/NTLM.

OWA and EWS live on the same server (`https://mail.company.com/owa` and `/EWS/Exchange.asmx`) and
take the same domain credentials, so "works in OWA" almost always means "works here". EWS is a
documented, stable API; OWA's own `service.svc` JSON calls are not (see README).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import requests
from zoneinfo import ZoneInfo

from exchangelib import (
    DELEGATE,
    Account,
    Body,
    CalendarItem,
    Configuration,
    Credentials,
    EWSDateTime,
    ExtendedProperty,
    HTMLBody,
    Mailbox,
    Q,
    close_connections,
)
from exchangelib import Message as EwsMessage
from exchangelib.errors import (
    ErrorAccessDenied,
    ErrorInvalidIdMalformed,
    ErrorItemNotFound,
    ErrorNameResolutionNoResults,
    TransportError,
    UnauthorizedError,
)
from exchangelib.items import Message as _EwsMessageType
from exchangelib.attachments import FileAttachment
from exchangelib.properties import Attendee, ItemId
from exchangelib.protocol import BaseProtocol
from exchangelib.transport import BASIC, DIGEST, GSSAPI, NTLM, SSPI

from .attachments import MAX_BYTES
from .backend import AttachmentData, AuthenticationError, BackendError, FolderScope, SearchQuery, SearchResult, pick_attachment
from .config import Settings
from .models import Event, Message, MessageSummary, Outgoing, Person
from .text import html_to_text, normalize_whitespace, preview
from .tls import PinningAdapter, apply_transport_settings

class _FlagStatus(ExtendedProperty):
    """PidTagFlagStatus, the follow-up flag OWA and Outlook show: 0 none, 1 complete, 2 flagged."""

    property_tag = 0x1090
    property_type = "Integer"


if "flag_status" not in {f.name for f in EwsMessage.FIELDS}:
    EwsMessage.register("flag_status", _FlagStatus)
_FLAG_VALUES = {"none": 0, "complete": 1, "flagged": 2}
_FOLDER_WORDS = {
    "inbox": "inbox", "входящие": "inbox", "sent": "sent", "отправленные": "sent", "drafts": "drafts", "черновики": "drafts",
    "deleted": "trash", "trash": "trash", "deleted items": "trash", "удалённые": "trash", "удаленные": "trash",
    "junk": "junk", "spam": "junk", "нежелательная почта": "junk",
}
_ARCHIVE_NAMES = ("archive", "архив")

# Every exchangelib session goes through the adapter that honours the certificates the user trusted.
BaseProtocol.HTTP_ADAPTER_CLS = PinningAdapter


def _owa_url(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    return f"{parsed.scheme}://{parsed.netloc}/owa/"


def warm_up(session, endpoint: str) -> None:  # noqa: ANN001
    """Signs in to OWA on this session, leaving an authenticated keep-alive connection in its pool.

    Some Exchange front ends (reverse proxies, WAF rules) drop EWS requests that arrive on a
    connection that has not signed in yet, without any answer, while OWA on the same host works.
    Windows sign-in (NTLM/Negotiate) belongs to the TCP connection, so a GET to /owa/ first
    authenticates the connection, and the EWS requests that follow ride it.
    """
    try:
        response = session.get(_owa_url(endpoint), allow_redirects=False, timeout=20)
        response.content  # noqa: B018 — read it all, so the connection goes back to the pool
    except requests.RequestException as exc:
        log.debug("OWA warm-up failed: %s: %s", type(exc).__name__, exc)


def _dropped_without_answer(exc: Exception) -> bool:
    text = repr(exc)
    return "RemoteDisconnected" in text or "Connection aborted" in text or "ConnectionResetError" in text


_original_create_session = BaseProtocol.create_session


def _create_session(self):  # noqa: ANN001
    session = _original_create_session(self)
    endpoint = self.service_endpoint
    # exchangelib mounts its adapter on the EWS URL only. /owa/ must go through the very same
    # adapter: same certificate pinning, and above all the same connection pool, or the
    # signed-in connection would end up where EWS requests never look.
    session.mount(_owa_url(endpoint), session.get_adapter(endpoint))
    warm_up(session, endpoint)
    plain_request = session.request

    def request(method, url, *args, **kwargs):
        ews = "/ews/" in url.lower()
        try:
            response = plain_request(method, url, *args, **kwargs)
        except requests.ConnectionError as exc:
            # Dropped with no answer at all: the request never reached Exchange (a fresh
            # connection the front end refused, or a keep-alive connection that timed out).
            # Sign in on a new connection and send it once more.
            if not ews or not _dropped_without_answer(exc):
                raise
            warm_up(session, endpoint)
            return plain_request(method, url, *args, **kwargs)
        if ews and response.status_code == 404:
            # Some front ends answer 404 instead of dropping an EWS request on a connection that
            # has not signed in to OWA. Sign in and try once more; a second 404 is reported.
            response.close()
            warm_up(session, endpoint)
            return plain_request(method, url, *args, **kwargs)
        return response

    session.request = request
    return session


BaseProtocol.create_session = _create_session

log = logging.getLogger(__name__)

_AUTH_TYPES = {"ntlm": NTLM, "basic": BASIC, "digest": DIGEST, "gssapi": GSSAPI, "kerberos": GSSAPI, "sspi": SSPI}
_SUMMARY_FIELDS = (
    "id", "subject", "sender", "datetime_received", "is_read", "has_attachments", "importance",
    "conversation_id", "body", "to_recipients", "cc_recipients", "flag_status",
)
_HEADER_FIELDS = ("id", "subject", "sender", "datetime_received", "is_read", "has_attachments", "importance", "conversation_id", "flag_status")
_FULL_FIELDS = _SUMMARY_FIELDS + ("attachments", "message_id", "in_reply_to", "categories", "conversation_topic")
_EVENT_FIELDS = (
    "id", "changekey", "subject", "start", "end", "is_all_day", "location", "organizer", "required_attendees",
    "optional_attendees", "is_meeting", "type", "is_cancelled", "my_response_type",
)
_SCAN_CAP = 500
_MAX_FOLDERS = 40
_EPOCH = EWSDateTime(1970, 1, 1, tzinfo=ZoneInfo("UTC"))
_FOLDER_CACHE_SECONDS = 600


def _http_failure(status: int) -> str:
    """The server answered, so it is reachable: say what the status means instead of blaming the network."""
    if status == 404:
        return ("Exchange answered HTTP 404 at the EWS address: the server is reachable, but EWS is not available "
                "there from this network. If it worked before, the usual cause is being outside the corporate network "
                "(VPN off or reconnecting): from outside, many companies publish only web mail. Otherwise the server "
                "address changed. Ask the user to check the VPN; if it is on, ask them to run `owa-mail-mcp diagnose` "
                "and share the output. Do not retry in a loop.")
    if status == 403:
        return ("Exchange answered HTTP 403 at the EWS address: access to EWS is forbidden for this account or from "
                "this network. Ask the user to check the VPN or ask their IT department whether EWS is allowed.")
    if status >= 500:
        return f"Exchange answered HTTP {status} (a server error). Try again in a few minutes."
    return f"Exchange answered HTTP {status} at the EWS address. Ask the user to run `owa-mail-mcp diagnose` and share the output."


def ews_endpoint(server: str) -> str:
    """`mail.company.com`, `https://mail.company.com/owa/` or a full EWS URL -> the EWS URL."""
    raw = server.strip()
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme != "https":
        raise BackendError("The server must use https: credentials are never sent over plain http.")
    if "/ews/" in (parsed.path.lower() + "/"):
        return raw
    return f"https://{parsed.netloc}/EWS/Exchange.asmx"


class CertificateProblem(BackendError):
    """The server's certificate is not trusted by the system or no longer matches the trusted one."""


class EWSBackend:
    name = "ews"

    def __init__(self, settings: Settings) -> None:
        missing = [n for n, v in (("OWA_MAIL_SERVER", settings.server), ("OWA_MAIL_EMAIL", settings.email), ("password", settings.password)) if not v]
        if missing:
            raise BackendError(
                "OWA Mail MCP is not connected to a mailbox yet (missing: " + ", ".join(missing) + ")."
            )
        auth = _AUTH_TYPES.get(settings.auth_type)
        if auth is None:
            raise BackendError(f"Unknown OWA_MAIL_AUTH {settings.auth_type!r}; use one of {', '.join(_AUTH_TYPES)}.")
        self._settings = settings
        apply_transport_settings(settings)
        # exchangelib caches protocols and keeps their connections open; a connection made under an
        # old pin (or old password) must not carry over to the new settings.
        close_connections()
        self._auth = auth
        self._endpoint = ews_endpoint(settings.server or "")
        self._account: Account | None = None
        self._lock = threading.Lock()
        self._folders: dict[str, tuple[float, list]] = {}

    # --- connection --------------------------------------------------------------------------

    @property
    def account(self) -> Account:
        # Built lazily: the MCP server must start (and list its tools) even with the VPN off.
        with self._lock:
            if self._account is None:
                s = self._settings
                config = Configuration(
                    service_endpoint=self._endpoint,
                    credentials=Credentials(username=s.username or s.email or "", password=s.password or ""),
                    auth_type=self._auth,
                )
                def connect() -> Account:
                    account = Account(primary_smtp_address=s.email, config=config, autodiscover=False, access_type=DELEGATE)
                    # exchangelib asks the server for its version lazily, but protocol-level calls such as
                    # resolve_names read it without asking and fail on None. Asking now also checks the
                    # password and the certificate on the first call, where the errors make sense.
                    account.version  # noqa: B018
                    return account

                self._account = self._call(connect)
            return self._account

    @staticmethod
    def _call(fn):
        try:
            return fn()
        except UnauthorizedError as exc:
            raise AuthenticationError(
                "Exchange rejected the credentials. Calls to Exchange are paused so a wrong password cannot lock "
                "the account. Ask the user to re-enter the password in OWA Mail MCP setup (`owa-mail-mcp setup`)."
            ) from exc
        except ErrorAccessDenied as exc:
            raise BackendError("Exchange denied access to this item or mailbox.") from exc
        except TransportError as exc:
            text = str(exc)
            if "CERTIFICATE_VERIFY_FAILED" in text or "Fingerprints did not match" in text or "certificate verify failed" in text:
                raise CertificateProblem(
                    "The Exchange server's certificate is not trusted (or changed since the user trusted it). "
                    "Ask the user to run `owa-mail-mcp setup` and check the certificate."
                ) from exc
            if "No valid version headers" in text or "No ServerVersionInfo" in text:
                raise BackendError(
                    "The server answered, but not like Exchange Web Services: check the server address "
                    "(the one you open webmail with) and that EWS is enabled."
                ) from exc
            status = re.search(r"Code: (\d{3})", text)
            if status:
                raise BackendError(_http_failure(int(status.group(1)))) from exc
            raise BackendError(f"Cannot reach Exchange: {text[:300]}. Is the VPN on?") from exc
        except (ConnectionError, TimeoutError) as exc:
            raise BackendError(f"Cannot reach Exchange ({type(exc).__name__}). Is the VPN on?") from exc

    # --- folders -----------------------------------------------------------------------------

    def _scope_folders(self, scope: FolderScope) -> list:
        a = self.account
        if scope == "inbox":
            return [a.inbox]
        if scope == "sent":
            return [a.sent]
        if scope == "inbox_and_sent":
            return [a.inbox, a.sent]
        cached = self._folders.get(scope)
        if cached and time.monotonic() - cached[0] < _FOLDER_CACHE_SECONDS:
            return cached[1]

        def walk():
            skip = set()
            for name in ("trash", "junk", "drafts", "outbox"):
                try:
                    folder = getattr(a, name)
                except Exception:  # a mailbox may lack one of them
                    continue
                if folder is not None:
                    skip.add(folder.id)
            out = []
            for folder in a.msg_folder_root.walk():
                if getattr(folder, "folder_class", None) == "IPF.Note" and folder.id not in skip:
                    out.append(folder)
            return out

        found = self._call(walk)
        # Inbox and Sent first: most searches end there, and the folder cap must not cut them.
        first = [a.inbox, a.sent]
        ids = {f.id for f in first}
        folders = (first + [f for f in found if f.id not in ids])[:_MAX_FOLDERS]
        self._folders[scope] = (time.monotonic(), folders)
        return folders

    def _fetch_many(self, ids: list[str], fields) -> list:
        """GetItem for message ids, with fields validated against a mail folder."""
        if not ids:
            return []
        return list(self.account.fetch(ids=[ItemId(id=i) for i in ids], folder=self.account.inbox, only_fields=list(fields)))

    def _find_per_folder(self, scope: FolderScope, make_qs, per_folder: int, accept=None) -> tuple[list, int]:
        """Runs the query in each folder separately and merges by date, newest first.

        Exchange refuses one FindItem over several folders when it sees the search as one in a
        shared mailbox ("Shared folder search cannot be performed on multiple folders"), which
        happens e.g. when the configured address is an alias of the mailbox. One folder at a time
        always works.
        """
        hits, scanned = [], 0
        for folder in self._scope_folders(scope):
            for item in make_qs(folder)[:per_folder]:
                scanned += 1
                if isinstance(item, _EwsMessageType) and (accept is None or accept(item)):
                    hits.append(item)
        hits.sort(key=lambda i: i.datetime_received or _EPOCH, reverse=True)
        return hits, scanned

    # --- mapping -----------------------------------------------------------------------------

    @staticmethod
    def _person(mailbox) -> Person | None:
        if mailbox is None:
            return None
        return Person(email=getattr(mailbox, "email_address", None), name=getattr(mailbox, "name", None))

    @staticmethod
    def _body_text(body) -> str:
        if body is None:
            return ""
        if isinstance(body, HTMLBody):
            return html_to_text(str(body))
        return normalize_whitespace(str(body))

    def _summary(self, item, *, with_preview: bool = True) -> MessageSummary:
        folder = getattr(item, "folder", None)
        body = self._body_text(getattr(item, "body", None)) if with_preview else ""
        conv = getattr(item, "conversation_id", None)
        return MessageSummary(
            id=item.id,
            subject=item.subject or "",
            sender=self._person(item.sender),
            received=item.datetime_received,
            folder=getattr(folder, "name", "") or "",
            is_read=bool(item.is_read),
            has_attachments=bool(item.has_attachments),
            importance=(item.importance or "Normal").lower(),
            preview=preview(body) if with_preview else "",
            conversation_id=getattr(conv, "id", None),
            flagged=getattr(item, "flag_status", None) == 2,
            to=[p for p in map(self._person, getattr(item, "to_recipients", None) or []) if p],
            cc=[p for p in map(self._person, getattr(item, "cc_recipients", None) or []) if p],
        )

    def _message(self, item) -> Message:
        summary = self._summary(item)
        return Message(
            **summary.__dict__,
            body=self._body_text(item.body),
            attachments=[getattr(att, "name", None) or "attachment" for att in (item.attachments or [])],
            internet_message_id=item.message_id,
            in_reply_to=item.in_reply_to,
            categories=list(item.categories or []),
        )

    def _fetch(self, message_id: str, fields=_FULL_FIELDS):
        def run():
            try:
                results = self._fetch_many([message_id], fields)
            except (ErrorItemNotFound, ErrorInvalidIdMalformed):
                results = []
            return results

        results = self._call(run)
        if not results or isinstance(results[0], Exception) or not isinstance(results[0], _EwsMessageType):
            raise BackendError("No message with this message_id. Take ids from check_mail, search_messages or get_thread.")
        return results[0]

    # --- MailBackend -------------------------------------------------------------------------

    def whoami(self) -> Person:
        a = self.account
        name = None
        try:
            resolved = self._call(lambda: a.protocol.resolve_names([a.primary_smtp_address]))
            if resolved and not isinstance(resolved[0], Exception):
                name = getattr(resolved[0], "name", None)
        except BackendError:
            pass
        return Person(email=a.primary_smtp_address, name=name)

    def unread_count(self) -> int | None:
        def run() -> int | None:
            inbox = self.account.inbox
            inbox.refresh()  # returns the folder itself, not None: keep it out of the return value
            count = inbox.unread_count
            return int(count) if count is not None else None

        return self._call(run)

    def search(self, query: SearchQuery) -> SearchResult:
        aqs = []
        if query.text:
            aqs.append(query.text.strip())
        if query.sender:
            aqs.append(f'from:"{_aqs_value(query.sender)}"')
        if query.recipient:
            aqs.append(f'participants:"{_aqs_value(query.recipient)}"')
        if query.subject:
            aqs.append(f'subject:"{_aqs_value(query.subject)}"')

        conditions = _date_conditions(query.since, query.until)
        if query.unread_only:
            conditions.append(Q(is_read=False))
        if query.has_attachments is not None:
            conditions.append(Q(has_attachments=query.has_attachments))

        def make_qs(folder):
            if aqs:
                # EWS takes either a search string or a restriction, not both: dates and flags are
                # then checked here, over the newest matches.
                qs = folder.filter(" ".join(aqs))
            else:
                qs = folder.filter(*conditions) if conditions else folder.all()
            # Headers first (one cheap FindItem per page), bodies only for what is returned.
            qs = qs.order_by("-datetime_received").only(*_HEADER_FIELDS)
            qs.page_size = 100
            return qs

        def run() -> SearchResult:
            per_folder = _SCAN_CAP if aqs else query.limit + 1
            hits, scanned = self._find_per_folder(query.folder, make_qs, per_folder, accept=(lambda i: _matches(i, query)) if aqs else None)
            complete = len(hits) <= query.limit and not (aqs and scanned >= _SCAN_CAP)
            hits = hits[: query.limit]
            folders = {item.id: getattr(getattr(item, "folder", None), "name", "") for item in hits}
            order = {item.id: n for n, item in enumerate(hits)}
            full = sorted(
                (i for i in self._fetch_many([item.id for item in hits], _SUMMARY_FIELDS) if isinstance(i, _EwsMessageType)),
                key=lambda i: order.get(i.id, 0),
            )
            found = []
            for item in full:
                if isinstance(item, _EwsMessageType):
                    summary = self._summary(item)
                    summary.folder = summary.folder or folders.get(item.id, "")
                    found.append(summary)
            return SearchResult(messages=found, scanned=scanned, complete=complete)

        return self._call(run)

    def get_message(self, message_id: str) -> Message:
        return self._message(self._fetch(message_id))

    def get_thread(self, message_id: str, max_messages: int) -> list[Message]:
        anchor = self._fetch(message_id, ("id", "conversation_id", "conversation_topic", "subject"))

        def make_qs(folder):
            if anchor.conversation_id is not None:
                qs = folder.filter(conversation_id=anchor.conversation_id)
            else:
                qs = folder.filter(conversation_topic=anchor.conversation_topic or anchor.subject or "")
            return qs.order_by("-datetime_received").only(*_HEADER_FIELDS)

        def run():
            hits, _ = self._find_per_folder("all", make_qs, max_messages)
            newest = [h.id for h in hits[:max_messages]]
            order = {mid: n for n, mid in enumerate(newest)}
            full = [i for i in self._fetch_many(newest, _FULL_FIELDS) if isinstance(i, _EwsMessageType)]
            full.sort(key=lambda i: order.get(i.id, 0), reverse=True)  # oldest first
            return [self._message(i) for i in full]

        messages = self._call(run)
        return messages or [self.get_message(message_id)]

    def scan_headers(self, folder: FolderScope, since: datetime | None, until: datetime | None, max_items: int) -> list[MessageSummary]:
        conditions = _date_conditions(since, until)
        # Recipients are not in FindItem's answer: for Sent they cost a GetItem per page.
        fields = _HEADER_FIELDS + (("to_recipients",) if folder == "sent" else ())

        def make_qs(f):
            qs = f.filter(*conditions) if conditions else f.all()
            qs = qs.order_by("-datetime_received").only(*fields)
            qs.page_size = 200
            return qs

        def run():
            hits, _ = self._find_per_folder(folder, make_qs, max_items)
            return [self._summary(i, with_preview=False) for i in hits[:max_items]]

        return self._call(run)

    def read_attachment(self, message_id: str, attachment: str) -> AttachmentData:
        item = self._fetch(message_id, ("id", "subject", "attachments"))
        att = pick_attachment(list(item.attachments or []), attachment, lambda a: getattr(a, "name", None) or "")
        name = getattr(att, "name", None) or "attachment"
        size = getattr(att, "size", None)
        if isinstance(att, FileAttachment):
            if size and size > MAX_BYTES:
                raise BackendError(f"{name} is {size // 1024 // 1024} MB; only files up to {MAX_BYTES // 1024 // 1024} MB are read.")
            data = self._call(lambda: att.content)
            return AttachmentData(name=name, content_type=att.content_type, size=size or len(data or b""), data=data)
        inner = getattr(att, "item", None)
        text = self._body_text(getattr(inner, "body", None)) if inner is not None else ""
        subject = getattr(inner, "subject", "") or ""
        return AttachmentData(name=name, content_type="message/rfc822", size=size, text=(f"Subject: {subject}\n\n" if subject else "") + text)

    def find_people(self, query: str, limit: int) -> list[Person]:
        a = self.account

        def run():
            try:
                return a.protocol.resolve_names(
                    [query], return_full_contact_data=True, search_scope="ActiveDirectoryContacts"
                )
            except ErrorNameResolutionNoResults:
                return []

        people: list[Person] = []
        for entry in self._call(run):
            if isinstance(entry, Exception):
                continue
            mailbox, contact = entry if isinstance(entry, tuple) else (entry, None)
            people.append(Person(
                email=getattr(mailbox, "email_address", None),
                name=getattr(contact, "display_name", None) or getattr(mailbox, "name", None),
                job_title=getattr(contact, "job_title", None),
                department=getattr(contact, "department", None),
                office=getattr(contact, "office", None),
            ))
            if len(people) >= limit:
                break
        return people

    # --- organizing ---------------------------------------------------------------------------

    def _mail_folders(self) -> list[tuple[str, Any]]:
        cached = self._folders.get("paths")
        if cached and time.monotonic() - cached[0] < _FOLDER_CACHE_SECONDS:
            return cached[1]

        def walk():
            root = self.account.msg_folder_root
            prefix = root.absolute.rstrip("/") + "/"
            return [(f.absolute[len(prefix):] if f.absolute.startswith(prefix) else f.name, f)
                    for f in root.walk() if getattr(f, "folder_class", None) in ("IPF.Note", None)]

        found = self._call(walk)
        self._folders["paths"] = (time.monotonic(), found)
        return found

    def _resolve_folder(self, name: str):
        key = " ".join(name.strip().strip("/").split()).lower()
        if key in _FOLDER_WORDS:
            return getattr(self.account, _FOLDER_WORDS[key])
        folders = self._mail_folders()
        if key in _ARCHIVE_NAMES:
            top = [f for path, f in folders if "/" not in path and path.lower() in _ARCHIVE_NAMES]
            if top:
                return top[0]
            raise BackendError("This mailbox has no Archive folder. Use a folder from list_folders, or ask the user to create one.")
        exact = [f for path, f in folders if path.lower() == key]
        named = [f for path, f in folders if path.rsplit("/", 1)[-1].lower() == key]
        if exact or len(named) == 1:
            return (exact or named)[0]
        hint = "several folders have this name; give the full path" if named else "no such folder"
        raise BackendError(f"Folder {name!r}: {hint}. Take a path from list_folders.")

    def list_folders(self) -> list[dict]:
        return [{"path": path, "unread": f.unread_count, "total": f.total_count}
                for path, f in self._mail_folders() if getattr(f, "folder_class", None) == "IPF.Note"]

    def save_draft(self, message: Outgoing) -> str | None:
        a = self.account
        to, cc, bcc = _mailboxes(message.to), _mailboxes(message.cc), _mailboxes(message.bcc)
        refers = message.forward_id or message.reply_to_id
        if refers:
            original = self._fetch(refers, ("id", "changekey", "subject"))
            make = original.create_forward if message.forward_id else original.create_reply
            self._call(lambda: make(subject=message.subject, body=message.body, to_recipients=to, cc_recipients=cc,
                                    bcc_recipients=bcc).save(a.drafts))
            return None
        draft = EwsMessage(account=a, folder=a.drafts, subject=message.subject, body=message.body,
                           to_recipients=to, cc_recipients=cc, bcc_recipients=bcc)
        self._call(draft.save)
        return draft.id

    def _items(self, message_ids: list[str], fields) -> list:
        def run():
            try:
                return self._fetch_many(message_ids, fields)
            except (ErrorItemNotFound, ErrorInvalidIdMalformed):
                return []

        items = [i for i in self._call(run) if isinstance(i, _EwsMessageType)]
        if not items:
            raise BackendError("None of these message_ids were found. Take ids from check_mail, search_messages or get_thread.")
        return items

    def update_messages(self, message_ids: list[str], *, read: bool | None, flag: str | None,
                        add_categories: list[str], remove_categories: list[str]) -> int:
        items = self._items(message_ids, ("id", "changekey", "is_read", "categories", "flag_status"))
        drop = {c.lower() for c in remove_categories}
        for item in items:
            fields = []
            if read is not None:
                item.is_read = read
                fields.append("is_read")
            if flag is not None:
                item.flag_status = _FLAG_VALUES[flag]
                fields.append("flag_status")
            if add_categories or remove_categories:
                categories = [c for c in (item.categories or []) if c.lower() not in drop]
                categories += [c for c in add_categories if c.lower() not in {x.lower() for x in categories}]
                item.categories = categories or None
                fields.append("categories")
            if fields:
                self._call(lambda item=item, fields=fields: item.save(update_fields=fields))
        return len(items)

    def move_messages(self, message_ids: list[str], folder: str) -> tuple[str, list[str]]:
        target = self._resolve_folder(folder)
        items = self._items(message_ids, ("id", "changekey"))
        for item in items:
            self._call(lambda item=item: item.move(target))
        return target.name, [item.id for item in items if item.id]

    # --- calendar ----------------------------------------------------------------------------

    def _event(self, item) -> Event:
        me = (self.account.primary_smtp_address or "").lower()
        organizer = self._person(item.organizer)
        attendees = []
        for attendee in list(item.required_attendees or []) + list(item.optional_attendees or []):
            person = self._person(getattr(attendee, "mailbox", None))
            if person and (person.email or "").lower() != me:
                attendees.append(person)
        response = item.my_response_type
        is_meeting = bool(item.is_meeting) and bool(attendees)
        is_organizer = (not is_meeting or response == "Organizer"
                        or bool(organizer and (organizer.email or "").lower() == me))
        return Event(
            id=item.id, subject=item.subject or "", start=_local(item.start), end=_local(item.end),
            is_all_day=bool(item.is_all_day), location=item.location or "", organizer=organizer,
            is_organizer=is_organizer, attendees=attendees, is_meeting=is_meeting,
            kind={"Occurrence": "occurrence", "Exception": "exception"}.get(item.type or "", "single"),
            is_cancelled=bool(item.is_cancelled), my_response=response or None,
        )

    def _event_item(self, event_id: str):
        def run():
            try:
                return list(self.account.fetch(ids=[ItemId(id=event_id)], folder=self.account.calendar, only_fields=list(_EVENT_FIELDS)))
            except (ErrorItemNotFound, ErrorInvalidIdMalformed):
                return []

        results = self._call(run)
        if not results or not isinstance(results[0], CalendarItem):
            raise BackendError("No calendar event with this event_id. Take ids from list_events.")
        return results[0]

    def list_events(self, start: datetime, end: datetime, limit: int) -> list[Event]:
        def run() -> list[Event]:
            view = self.account.calendar.view(start=_ews_dt(start), end=_ews_dt(end), max_items=max(limit, 1))
            return [self._event(item) for item in view.only(*_EVENT_FIELDS) if isinstance(item, CalendarItem)]

        events = self._call(run)
        events.sort(key=lambda e: e.start or start)
        return events[:limit]

    def get_event(self, event_id: str) -> Event:
        return self._event(self._event_item(event_id))

    def cancel_event(self, event_id: str, message: str) -> str:
        item = self._event_item(event_id)
        event = self._event(item)
        if not event.is_meeting:
            self._call(lambda: item.delete(send_meeting_cancellations="SendToNone"))
            return "deleted"
        note = Body(message) if message else None
        if event.is_organizer:
            self._call(lambda: item.cancel(new_body=note))
            return "cancelled"
        self._call(lambda: item.decline(body=note))
        return "declined"

    def create_event(self, subject: str, start: datetime, end: datetime, attendees: list[str], location: str, body: str) -> Event:
        a = self.account
        item = CalendarItem(
            account=a, folder=a.calendar, subject=subject, start=_ews_dt(start), end=_ews_dt(end), location=location or None,
            body=body or None, required_attendees=[Attendee(mailbox=Mailbox(email_address=x)) for x in attendees] or None,
        )
        invitations = "SendToAllAndSaveCopy" if attendees else "SendToNone"
        self._call(lambda: item.save(send_meeting_invitations=invitations))
        return self.get_event(item.id)

    def reschedule_event(self, event_id: str, start: datetime, end: datetime) -> None:
        item = self._event_item(event_id)
        event = self._event(item)
        if event.is_meeting and not event.is_organizer:
            raise BackendError("Only the organizer can move this meeting. Offer the user to write to the organizer instead.")
        item.start, item.end = _ews_dt(start), _ews_dt(end)
        invitations = "SendToAllAndSaveCopy" if event.is_meeting else "SendToNone"
        self._call(lambda: item.save(update_fields=["start", "end"], send_meeting_invitations=invitations))

    def send(self, message: Outgoing) -> str | None:
        a = self.account
        to = [Mailbox(email_address=x) for x in message.to]
        cc = [Mailbox(email_address=x) for x in message.cc] or None
        bcc = [Mailbox(email_address=x) for x in message.bcc] or None
        if message.forward_id:
            original = self._fetch(message.forward_id, ("id", "changekey", "subject"))
            # Exchange attaches the original's attachments and quotes it below, as Outlook does.
            self._call(lambda: original.forward(subject=message.subject, body=message.body, to_recipients=to, cc_recipients=cc, bcc_recipients=bcc))
            return None
        if message.reply_to_id:
            original = self._fetch(message.reply_to_id, ("id", "changekey", "subject"))
            # Exchange quotes the original under the reply, as Outlook does.
            self._call(lambda: original.reply(subject=message.subject, body=message.body, to_recipients=to, cc_recipients=cc, bcc_recipients=bcc))
            return None
        outgoing = EwsMessage(account=a, folder=a.sent, subject=message.subject, body=message.body, to_recipients=to, cc_recipients=cc, bcc_recipients=bcc)
        self._call(outgoing.send_and_save)
        return None


def _mailboxes(addresses: list[str]) -> list[Mailbox] | None:
    return [Mailbox(email_address=x) for x in addresses] or None


def _local(value) -> datetime | None:
    """An EWS date-time as an aware datetime in the user's local zone."""
    if value is None:
        return None
    if not isinstance(value, datetime):  # an all-day event may come as a plain date
        return datetime(value.year, value.month, value.day).astimezone()
    return datetime.fromtimestamp(value.timestamp()).astimezone()


def _ews_dt(value: datetime) -> EWSDateTime:
    # Through UTC: exchangelib wants a zoneinfo zone, not the fixed offset `astimezone()` gives.
    return EWSDateTime.from_datetime(value.astimezone(ZoneInfo("UTC")))


def _date_conditions(since: datetime | None, until: datetime | None) -> list[Q]:
    out = []
    if since:
        out.append(Q(datetime_received__gte=_ews_dt(since)))
    if until:
        out.append(Q(datetime_received__lt=_ews_dt(until)))
    return out


def _aqs_value(value: str) -> str:
    return value.replace('"', " ").strip()


def _matches(item, query: SearchQuery) -> bool:
    received = item.datetime_received
    if query.since and received and received < query.since:
        return False
    if query.until and received and received >= query.until:
        return False
    if query.unread_only and item.is_read:
        return False
    if query.has_attachments is not None and bool(item.has_attachments) != query.has_attachments:
        return False
    return True
