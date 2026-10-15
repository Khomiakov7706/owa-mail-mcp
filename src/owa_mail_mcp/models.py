"""Plain data the backends return and the tools serialize. No Exchange types leak past here."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


@dataclass(frozen=True)
class Person:
    email: str | None
    name: str | None = None
    job_title: str | None = None
    department: str | None = None
    office: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "email": self.email}
        for key in ("job_title", "department", "office"):
            value = getattr(self, key)
            if value:
                out[key] = value
        return out


@dataclass
class MessageSummary:
    id: str
    subject: str
    sender: Person | None
    received: datetime | None
    folder: str
    is_read: bool = True
    has_attachments: bool = False
    importance: str = "normal"
    preview: str = ""
    conversation_id: str | None = None
    to: list[Person] = field(default_factory=list)
    cc: list[Person] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "message_id": self.id,
            "subject": self.subject,
            "from": self.sender.to_dict() if self.sender else None,
            "received": _iso(self.received),
            "folder": self.folder,
            "is_read": self.is_read,
            "has_attachments": self.has_attachments,
            "preview": self.preview,
        }
        if self.importance and self.importance != "normal":
            out["importance"] = self.importance
        return out


@dataclass
class Message(MessageSummary):
    body: str = ""
    """Plain text, as the server gave it or converted from HTML. Quoted history included."""
    attachments: list[str] = field(default_factory=list)
    internet_message_id: str | None = None
    in_reply_to: str | None = None
    categories: list[str] = field(default_factory=list)


@dataclass
class Outgoing:
    """A message the agent proposes. What the user approves in the window is what gets sent."""

    to: list[str]
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    subject: str = ""
    body: str = ""
    reply_to_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "to": list(self.to),
            "cc": list(self.cc),
            "bcc": list(self.bcc),
            "subject": self.subject,
            "body": self.body,
            "reply_to_message_id": self.reply_to_id,
        }

    def fingerprint(self) -> tuple:
        norm = lambda xs: tuple(sorted(x.lower() for x in xs))  # noqa: E731
        return (norm(self.to), norm(self.cc), norm(self.bcc), self.subject.strip(), self.body.strip(), self.reply_to_id)
