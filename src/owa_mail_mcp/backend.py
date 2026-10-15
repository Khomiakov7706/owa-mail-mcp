"""What the MCP tools need from a mailbox. `EWSBackend` talks to Exchange, `DemoBackend` is in memory.

Backends are synchronous (exchangelib is); the server runs them in worker threads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from .models import Message, MessageSummary, Outgoing, Person

FolderScope = Literal["inbox", "sent", "inbox_and_sent", "all"]


class BackendError(Exception):
    """A failure the agent should read: bad id, server said no, and so on."""


class AuthenticationError(BackendError):
    """The server rejected the credentials. The server stops calling Exchange after this."""


@dataclass
class SearchQuery:
    text: str | None = None
    sender: str | None = None
    recipient: str | None = None
    subject: str | None = None
    folder: FolderScope = "inbox"
    since: datetime | None = None
    until: datetime | None = None
    unread_only: bool = False
    has_attachments: bool | None = None
    limit: int = 20


@dataclass
class SearchResult:
    messages: list[MessageSummary]
    scanned: int
    """How many messages were looked at; when it hit the scan cap, older matches may be missing."""
    complete: bool


class MailBackend(Protocol):
    name: str

    def whoami(self) -> Person: ...

    def unread_count(self) -> int | None: ...

    def search(self, query: SearchQuery) -> SearchResult: ...

    def get_message(self, message_id: str) -> Message: ...

    def get_thread(self, message_id: str, max_messages: int) -> list[Message]:
        """All messages of the conversation the message belongs to, in Inbox, Sent and other mail folders,
        oldest first."""
        ...

    def scan_headers(self, folder: FolderScope, since: datetime | None, until: datetime | None, max_items: int) -> list[MessageSummary]:
        """Newest first, without bodies. Feeds the sender statistics."""
        ...

    def find_people(self, query: str, limit: int) -> list[Person]:
        """The organization's address book (GAL) and the user's contacts."""
        ...

    def send(self, message: Outgoing) -> str | None:
        """Sends and saves to Sent Items. Returns the sent copy's id when the server reports one."""
        ...
