"""Pending sends and calendar changes waiting for the user's answer in the confirmation window.

The answer never comes from the MCP client: a model can be talked into anything by an email it has
read. Only the local window (a page only the user's browser has the address of) decides.

Thread-safe: tool calls wait in worker threads, the window's HTTP server answers from its own.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .i18n import Coded
from .models import Outgoing
from .safety import OutgoingReview


class Status(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    SENT = "sent"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    """The client cancelled the call or went away."""


@dataclass
class Proposal:
    id: str
    message: Outgoing | None
    """The email to send; None for a calendar change."""
    review: OutgoingReview
    client: str
    sender: str
    created: float
    deadline: float
    reply_context: dict[str, Any] | None = None
    status: Status = Status.PENDING
    edited: bool = False
    error: str | None = None
    finished: float | None = None
    action: dict[str, Any] | None = None
    """A calendar change: `kind` (cancel, decline, delete, reschedule), the `event`, and `new_start`/`new_end` or `note`."""
    auto: bool = False
    """Sent without asking: every recipient is on the user's trusted list."""
    via: str = "window"
    """Where the user said yes: `window`, `trusted` (no question asked) or `chat` (the MCP client asked them)."""

    def to_dict(self, now: float) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status.value,
            "client": self.client,
            "from": self.sender,
            "message": self.message.to_dict() if self.message else None,
            "action": self.action,
            "auto": self.auto,
            "via": self.via,
            "external": self.review.external,
            "warnings": self.review.warnings,
            "warning_codes": [getattr(w, "code", "") for w in self.review.warnings],
            "reply_context": self.reply_context,
            "seconds_left": max(0, int(self.deadline - now)) if self.status == Status.PENDING else 0,
            "edited": self.edited,
            "error": self.error,
        }


class BrokerBusy(Exception):
    pass


class ConfirmationBroker:
    def __init__(
        self,
        review: Callable[[Outgoing], OutgoingReview],
        *,
        review_action: Callable[[dict[str, Any], dict[str, Any]], tuple[dict[str, Any], list[str]]] | None = None,
        max_pending: int = 3,
        history: int = 20,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._review = review
        self._review_action = review_action
        self._max_pending = max_pending
        self._history = history
        self._clock = clock
        self._cond = threading.Condition()
        self._items: dict[str, Proposal] = {}
        self._order: list[str] = []
        self._ui_seen = 0.0

    # --- tool side ---------------------------------------------------------------------------

    def submit(self, message: Outgoing | None, review: OutgoingReview, *, client: str, sender: str, timeout: float, reply_context=None,
               action: dict[str, Any] | None = None) -> Proposal:
        with self._cond:
            self._expire_due()
            if sum(1 for p in self._items.values() if p.status == Status.PENDING) >= self._max_pending:
                raise BrokerBusy(f"{self._max_pending} requests are already waiting for the user's answer. Wait for them first.")
            now = self._clock()
            proposal = Proposal(
                id=secrets.token_urlsafe(9), message=message, review=review, client=client, sender=sender,
                created=now, deadline=now + timeout, reply_context=reply_context, action=action,
            )
            self._items[proposal.id] = proposal
            self._order.append(proposal.id)
            self._trim()
            self._cond.notify_all()
            return proposal

    def record_trusted(self, message: Outgoing | None, review: OutgoingReview, *, client: str, sender: str, reply_context=None,
                       action: dict[str, Any] | None = None, via: str = "trusted") -> Proposal:
        """Approved elsewhere (trusted recipients, or the user answered in the chat): listed in the window's history."""
        with self._cond:
            now = self._clock()
            proposal = Proposal(
                id=secrets.token_urlsafe(9), message=message, review=review, client=client, sender=sender, created=now,
                deadline=now, reply_context=reply_context, status=Status.APPROVED, finished=now, auto=via == "trusted",
                action=action, via=via,
            )
            self._items[proposal.id] = proposal
            self._order.append(proposal.id)
            self._trim()
            return proposal

    def wait(self, proposal_id: str, timeout: float) -> Status:
        """Blocks up to `timeout` seconds for an answer; returns the status then."""
        end = self._clock() + timeout
        with self._cond:
            while True:
                self._expire_due()
                proposal = self._items[proposal_id]
                if proposal.status != Status.PENDING:
                    return proposal.status
                remaining = min(end, proposal.deadline) - self._clock()
                if remaining <= 0:
                    if self._clock() < proposal.deadline:
                        return Status.PENDING
                    continue
                self._cond.wait(remaining)

    def get(self, proposal_id: str) -> Proposal:
        with self._cond:
            return self._items[proposal_id]

    def cancel(self, proposal_id: str) -> None:
        self._finish(proposal_id, Status.CANCELLED, only_from=(Status.PENDING,))

    def mark_sent(self, proposal_id: str) -> None:
        self._finish(proposal_id, Status.SENT, only_from=(Status.APPROVED,))

    def mark_failed(self, proposal_id: str, error: str) -> None:
        self._finish(proposal_id, Status.FAILED, only_from=(Status.APPROVED,), error=error)

    # --- window side -------------------------------------------------------------------------

    def decide(self, proposal_id: str, approve: bool, edited: Outgoing | None = None,
               action_edits: dict[str, Any] | None = None) -> str | None:
        """The user's button. Returns an error to show in the window, or None."""
        with self._cond:
            self._expire_due()
            proposal = self._items.get(proposal_id)
            if proposal is None:
                return Coded("This message is no longer waiting.", "gone")
            if proposal.status != Status.PENDING:
                return Coded(f"Already {proposal.status.value}.", "already")
            if approve and edited is not None and proposal.message is not None:
                try:
                    review = self._review(edited)
                except ValueError as exc:
                    return Coded(str(exc), getattr(exc, "code", ""), **getattr(exc, "params", {}))
                if edited.fingerprint() != proposal.message.fingerprint():
                    proposal.edited = True
                proposal.message, proposal.review = edited, review
            if approve and action_edits and proposal.action is not None and self._review_action is not None:
                try:
                    action, external = self._review_action(proposal.action, action_edits)
                except ValueError as exc:
                    return Coded(str(exc), getattr(exc, "code", ""), **getattr(exc, "params", {}))
                if action != proposal.action:
                    proposal.edited = True
                proposal.action, proposal.review = action, OutgoingReview(external=external)
            proposal.status = Status.APPROVED if approve else Status.REJECTED
            proposal.finished = self._clock()
            self._cond.notify_all()
            return None

    def snapshot(self) -> list[dict[str, Any]]:
        with self._cond:
            self._ui_seen = self._clock()
            self._expire_due()
            now = self._clock()
            return [self._items[i].to_dict(now) for i in reversed(self._order)]

    def ui_seen_within(self, seconds: float) -> bool:
        with self._cond:
            return self._clock() - self._ui_seen < seconds

    # --- internals ---------------------------------------------------------------------------

    def _finish(self, proposal_id: str, status: Status, *, only_from: tuple[Status, ...], error: str | None = None) -> None:
        with self._cond:
            proposal = self._items.get(proposal_id)
            if proposal is None or proposal.status not in only_from:
                return
            proposal.status = status
            proposal.error = error
            proposal.finished = self._clock()
            self._cond.notify_all()

    def _expire_due(self) -> None:
        now = self._clock()
        for proposal in self._items.values():
            if proposal.status == Status.PENDING and now >= proposal.deadline:
                proposal.status = Status.EXPIRED
                proposal.finished = now
                self._cond.notify_all()

    def _trim(self) -> None:
        while len(self._order) > self._history:
            oldest = next((i for i in self._order if self._items[i].status != Status.PENDING), None)
            if oldest is None:
                return
            self._order.remove(oldest)
            del self._items[oldest]
