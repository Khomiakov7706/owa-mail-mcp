"""What the MCP tools need from a mailbox. `EWSBackend` talks to Exchange, `DemoBackend` is in memory.

Backends are synchronous (exchangelib is); the server runs them in worker threads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from .models import Event, Message, MessageSummary, Outgoing, Person

FolderScope = Literal["inbox", "sent", "inbox_and_sent", "all"]


class BackendError(Exception):
    """A failure the agent should read: bad id, server said no, and so on."""


def pick_attachment(attachments: list, wanted: str, name_of) -> object:
    """By exact name, then case-insensitive, then by position ("2" or "#2")."""
    if not attachments:
        raise BackendError("This message has no attachments.")
    wanted = (wanted or "").strip()
    for match in (lambda n: n == wanted, lambda n: n.lower() == wanted.lower()):
        for att in attachments:
            if match(name_of(att)):
                return att
    number = wanted.lstrip("#")
    if number.isdigit() and 1 <= int(number) <= len(attachments):
        return attachments[int(number) - 1]
    names = ", ".join(repr(name_of(a)) for a in attachments)
    raise BackendError(f"No attachment {wanted!r}. This message has: {names}.")


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


@dataclass
class AttachmentData:
    name: str
    content_type: str | None
    size: int | None
    data: bytes | None = None
    """The file's bytes; None for an attached email, whose text is in `text`."""
    text: str | None = None


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

    def read_attachment(self, message_id: str, attachment: str) -> AttachmentData:
        """`attachment`: the file name as get_message lists it, or its position (1, 2, ...)."""
        ...

    def find_people(self, query: str, limit: int) -> list[Person]:
        """The organization's address book (GAL) and the user's contacts."""
        ...

    def send(self, message: Outgoing) -> str | None:
        """Sends and saves to Sent Items. Returns the sent copy's id when the server reports one."""
        ...

    # --- calendar ----------------------------------------------------------------------------

    def list_events(self, start: datetime, end: datetime, limit: int) -> list[Event]:
        """Calendar entries overlapping [start, end), repeating meetings expanded into occurrences, earliest first."""
        ...

    def get_event(self, event_id: str) -> Event: ...

    def cancel_event(self, event_id: str, message: str) -> str:
        """Organizer: cancels the meeting and notifies the attendees (`cancelled`). Attendee: declines it and
        tells the organizer (`declined`). A plain appointment is deleted (`deleted`)."""
        ...

    def create_event(self, subject: str, start: datetime, end: datetime, attendees: list[str], location: str, body: str,
                     optional: list[str] | None = None) -> Event:
        """Creates a meeting (invitations go to the attendees) or, without attendees, an appointment."""
        ...

    def event_details(self, event_id: str) -> dict:
        """`event` (Event), `agenda` (text), `attendees` ({name, email, role: required|optional, response}), `join_url`."""
        ...

    def free_busy(self, emails: list[str], start: datetime, end: datetime) -> dict[str, list[tuple[datetime, datetime]] | None]:
        """Busy intervals (busy, tentative, out of office) of each mailbox; None when Exchange has no data for it."""
        ...

    def reschedule_event(self, event_id: str, start: datetime, end: datetime) -> None:
        """Moves the event; for a meeting the attendees get the updated invitation. Organizer only."""
        ...

    # --- organizing ---------------------------------------------------------------------------

    def list_folders(self) -> list[dict]:
        """Mail folders: `path` (as move_messages takes it), `unread`, `total`."""
        ...

    def save_draft(self, message: Outgoing) -> str | None:
        """Saves to Drafts (a reply or forward draft when the message refers to one). Returns its id when known."""
        ...

    def update_messages(self, message_ids: list[str], *, read: bool | None, flag: str | None,
                        add_categories: list[str], remove_categories: list[str]) -> int:
        """`flag`: flagged, complete or none. Returns how many messages were changed."""
        ...

    def move_messages(self, message_ids: list[str], folder: str) -> tuple[str, list[str]]:
        """`folder`: inbox, archive, deleted, junk or a folder path from list_folders. Returns the folder's path and
        the messages' new ids (Exchange gives a moved message a new id)."""
        ...

