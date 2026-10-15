"""Exchange Web Services through exchangelib: on-prem Exchange 2013/2016/2019 and Exchange Online with Basic/NTLM.

OWA and EWS live on the same server (`https://mail.company.com/owa` and `/EWS/Exchange.asmx`) and
take the same domain credentials, so "works in OWA" almost always means "works here". EWS is a
documented, stable API; OWA's own `service.svc` JSON calls are not (see README).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from exchangelib import (
    DELEGATE,
    Account,
    Configuration,
    Credentials,
    EWSDateTime,
    FolderCollection,
    HTMLBody,
    Mailbox,
    Q,
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
from exchangelib.properties import ItemId
from exchangelib.protocol import BaseProtocol
from exchangelib.transport import BASIC, DIGEST, GSSAPI, NTLM, SSPI

from .backend import AuthenticationError, BackendError, FolderScope, SearchQuery, SearchResult
from .config import Settings
from .models import Message, MessageSummary, Outgoing, Person
from .text import html_to_text, normalize_whitespace, preview
from .tls import PinningAdapter

# Every exchangelib session goes through the adapter that honours the certificates the user trusted.
BaseProtocol.HTTP_ADAPTER_CLS = PinningAdapter

log = logging.getLogger(__name__)

_AUTH_TYPES = {"ntlm": NTLM, "basic": BASIC, "digest": DIGEST, "gssapi": GSSAPI, "kerberos": GSSAPI, "sspi": SSPI}
_SUMMARY_FIELDS = (
    "id", "subject", "sender", "datetime_received", "is_read", "has_attachments", "importance",
    "conversation_id", "body", "to_recipients", "cc_recipients",
)
_HEADER_FIELDS = ("id", "subject", "sender", "datetime_received", "is_read", "has_attachments", "importance", "conversation_id")
_FULL_FIELDS = _SUMMARY_FIELDS + ("attachments", "internet_message_id", "in_reply_to", "categories", "conversation_topic")
_SCAN_CAP = 500
_FOLDER_CACHE_SECONDS = 600


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
        PinningAdapter.pins = dict(settings.trusted_certificates)
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
                self._account = self._call(lambda: Account(
                    primary_smtp_address=s.email, config=config, autodiscover=False, access_type=DELEGATE,
                ))
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
            raise BackendError(f"Cannot reach Exchange ({type(exc).__name__}). Is the VPN on?") from exc
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
        skip = {f.id for f in (a.trash, a.junk, a.drafts, a.outbox) if f is not None}

        def walk():
            out = []
            for folder in a.msg_folder_root.walk():
                if getattr(folder, "folder_class", None) == "IPF.Note" and folder.id not in skip:
                    out.append(folder)
            return out

        folders = self._call(walk)
        ids = {f.id for f in folders}
        folders += [f for f in (a.inbox, a.sent) if f.id not in ids]
        self._folders[scope] = (time.monotonic(), folders)
        return folders

    def _collection(self, scope: FolderScope) -> FolderCollection:
        return FolderCollection(account=self.account, folders=self._scope_folders(scope))

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
            to=[p for p in map(self._person, getattr(item, "to_recipients", None) or []) if p],
            cc=[p for p in map(self._person, getattr(item, "cc_recipients", None) or []) if p],
        )

    def _message(self, item) -> Message:
        summary = self._summary(item)
        return Message(
            **summary.__dict__,
            body=self._body_text(item.body),
            attachments=[getattr(att, "name", None) or "attachment" for att in (item.attachments or [])],
            internet_message_id=item.internet_message_id,
            in_reply_to=item.in_reply_to,
            categories=list(item.categories or []),
        )

    def _fetch(self, message_id: str, fields=_FULL_FIELDS):
        def run():
            try:
                results = list(self.account.fetch(ids=[ItemId(id=message_id)], only_fields=list(fields)))
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
        return self._call(lambda: self.account.inbox.refresh() or self.account.inbox.unread_count)

    def search(self, query: SearchQuery) -> SearchResult:
        collection = self._collection(query.folder)
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

        def run() -> SearchResult:
            if aqs:
                # EWS takes either a search string or a restriction, not both: dates and flags are
                # then checked here, over the newest matches.
                qs = collection.filter(" ".join(aqs))
            else:
                qs = collection.filter(*conditions) if conditions else collection.all()
            # Headers first (one cheap FindItem per page), bodies only for what is returned.
            qs = qs.order_by("-datetime_received").only(*_HEADER_FIELDS)
            qs.page_size = 100
            hits = []
            scanned = 0
            for item in qs[:_SCAN_CAP] if aqs else qs[: query.limit + 1]:
                scanned += 1
                if isinstance(item, _EwsMessageType) and (not aqs or _matches(item, query)):
                    hits.append(item)
                    if len(hits) > query.limit:
                        break
            complete = len(hits) <= query.limit and not (aqs and scanned >= _SCAN_CAP)
            hits = hits[: query.limit]
            folders = {item.id: getattr(getattr(item, "folder", None), "name", "") for item in hits}
            full = self.account.fetch(ids=[ItemId(id=item.id) for item in hits], only_fields=list(_SUMMARY_FIELDS)) if hits else []
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
        collection = self._collection("all")

        def run():
            if anchor.conversation_id is not None:
                qs = collection.filter(conversation_id=anchor.conversation_id)
            else:
                qs = collection.filter(conversation_topic=anchor.conversation_topic or anchor.subject or "")
            items = list(qs.order_by("-datetime_received").only(*_FULL_FIELDS)[:max_messages])
            return [self._message(i) for i in reversed(items) if isinstance(i, _EwsMessageType)]

        messages = self._call(run)
        return messages or [self.get_message(message_id)]

    def scan_headers(self, folder: FolderScope, since: datetime | None, until: datetime | None, max_items: int) -> list[MessageSummary]:
        conditions = _date_conditions(since, until)
        collection = self._collection(folder)

        def run():
            qs = collection.filter(*conditions) if conditions else collection.all()
            # Recipients are not in FindItem's answer: for Sent they cost a GetItem per page.
            fields = _HEADER_FIELDS + (("to_recipients",) if folder == "sent" else ())
            qs = qs.order_by("-datetime_received").only(*fields)
            qs.page_size = 200
            return [self._summary(i, with_preview=False) for i in qs[:max_items] if isinstance(i, _EwsMessageType)]

        return self._call(run)

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

    def send(self, message: Outgoing) -> str | None:
        a = self.account
        to = [Mailbox(email_address=x) for x in message.to]
        cc = [Mailbox(email_address=x) for x in message.cc] or None
        bcc = [Mailbox(email_address=x) for x in message.bcc] or None
        if message.reply_to_id:
            original = self._fetch(message.reply_to_id, ("id", "changekey", "subject"))
            # Exchange quotes the original under the reply, as Outlook does.
            self._call(lambda: original.reply(subject=message.subject, body=message.body, to_recipients=to, cc_recipients=cc, bcc_recipients=bcc))
            return None
        outgoing = EwsMessage(account=a, folder=a.sent, subject=message.subject, body=message.body, to_recipients=to, cc_recipients=cc, bcc_recipients=bcc)
        self._call(outgoing.send_and_save)
        return None


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
