"""What protects the mailbox and the people in it from a confused or manipulated agent.

- `check_outgoing`: refuses text that would display differently from what is sent, invalid or too
  many addresses; flags recipients outside the user's domain.
- `ExchangeGuard`: a rate limit, and a latch that stops all calls after Exchange rejects the
  password, so an agent retrying in a loop cannot walk a domain account into an AD lockout.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .i18n import Coded
from .models import Outgoing
from .text import contains_bidi_controls, contains_hidden_characters, domain_of, is_valid_address, normalize_body


class PolicyError(ValueError):
    """English for the agent, plus a message key the confirmation window shows in the user's language."""

    def __init__(self, text: str, code: str = "", **params) -> None:
        super().__init__(text)
        self.code, self.params = code, params


@dataclass
class OutgoingReview:
    external: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def clean_addresses(values: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        for part in str(raw).replace(";", ",").split(","):
            email = part.strip().strip("<>").strip()
            if email and email.lower() not in seen:
                seen.add(email.lower())
                out.append(email)
    return out


def check_outgoing(message: Outgoing, *, my_email: str | None, internal_domains: tuple[str, ...] = (), max_recipients: int = 50) -> OutgoingReview:
    """Validates in place (normalizes the body) and returns what the window must point out."""
    message.to = clean_addresses(message.to)
    message.cc = clean_addresses(message.cc)
    message.bcc = clean_addresses(message.bcc)
    message.subject = " ".join(message.subject.split())
    message.body = normalize_body(message.body)

    everyone = message.to + message.cc + message.bcc
    if not message.to:
        raise PolicyError("At least one recipient in `to` is required.", "no_to")
    if len(everyone) > max_recipients:
        raise PolicyError(f"Too many recipients ({len(everyone)}); the limit is {max_recipients}.", "too_many", n=len(everyone), limit=max_recipients)
    bad = [e for e in everyone if not is_valid_address(e)]
    if bad:
        raise PolicyError(
            "Not a plain email address: " + ", ".join(repr(b) for b in bad[:5]) +
            ". Use addresses from find_people or from the messages themselves; only ASCII addresses are accepted.",
            "bad_address", list=", ".join(bad[:5]),
        )
    if not message.subject:
        raise PolicyError("The subject is empty.", "no_subject")
    if len(message.subject) > 255:
        raise PolicyError("The subject is longer than 255 characters.", "subject_long")
    if not message.body:
        raise PolicyError("The body is empty.", "no_body")
    if len(message.body) > 100_000:
        raise PolicyError("The body is longer than 100 000 characters.", "body_long")
    for label, text in (("subject", message.subject), ("body", message.body)):
        if contains_bidi_controls(text):
            raise PolicyError(f"The {label} contains bidirectional control characters, which make text display differently from what is sent.", f"bidi_{label}")

    review = OutgoingReview()
    mine = domain_of(my_email or "")
    internal = {d.lower() for d in internal_domains} | ({mine} if mine else set())
    if internal:
        review.external = [e for e in everyone if domain_of(e) not in internal and not any(domain_of(e).endswith("." + d) for d in internal)]
    else:
        review.warnings.append(Coded("Your own mail domain is unknown, so external recipients cannot be flagged.", "unknown_domain"))
    if my_email and any(e.lower() == my_email.lower() for e in everyone):
        review.warnings.append(Coded("You are among the recipients.", "self"))
    if contains_hidden_characters(message.subject) or contains_hidden_characters(message.body):
        review.warnings.append(Coded("The text contains invisible characters (zero-width spaces or similar).", "invisible"))
    if message.bcc:
        review.warnings.append(Coded("There are hidden copies (Bcc).", "bcc"))
    return review


class ExchangeGuard:
    def __init__(self, requests_per_minute: int = 120, clock=time.monotonic) -> None:
        self._capacity = max(1, requests_per_minute)
        self._tokens = float(self._capacity)
        self._clock = clock
        self._updated = clock()
        self._lock = threading.Lock()
        self.blocked_reason: str | None = None

    def permit(self, cost: int = 1) -> str | None:
        """None when the call may go ahead, otherwise why not."""
        with self._lock:
            if self.blocked_reason:
                return self.blocked_reason
            now = self._clock()
            self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._capacity / 60.0)
            self._updated = now
            if self._tokens < cost:
                return "Too many requests to Exchange in the last minute. Wait a minute or narrow the request."
            self._tokens -= cost
            return None

    def block(self, reason: str) -> None:
        with self._lock:
            self.blocked_reason = reason
