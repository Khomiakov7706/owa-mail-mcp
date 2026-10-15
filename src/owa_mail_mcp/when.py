"""Dates in tool arguments: `2026-10-01`, ISO date-times, `today`, `yesterday`, `7d`, `12h`."""

from __future__ import annotations

import re
from datetime import datetime, timedelta


def now() -> datetime:
    return datetime.now().astimezone()


def parse_when(value: str | None, *, end_of_day: bool = False, reference: datetime | None = None) -> datetime | None:
    """Local-time aware datetime, or None for an empty value. Raises ValueError with a readable message.

    A bare date means the start of that day, or its end when `end_of_day` (for `until`).
    """
    if value is None or not str(value).strip():
        return None
    text = str(value).strip().lower()
    ref = reference or now()
    midnight = ref.replace(hour=0, minute=0, second=0, microsecond=0)

    if text in {"now"}:
        return ref
    if text in {"today", "сегодня"}:
        return midnight + timedelta(days=1) if end_of_day else midnight
    if text in {"yesterday", "вчера"}:
        day = midnight - timedelta(days=1)
        return day + timedelta(days=1) if end_of_day else day
    match = re.fullmatch(r"(\d{1,4})\s*([hdw])", text)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        delta = {"h": timedelta(hours=amount), "d": timedelta(days=amount), "w": timedelta(weeks=amount)}[unit]
        return ref - delta
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        day = datetime.fromisoformat(text).replace(tzinfo=ref.tzinfo)
        return day + timedelta(days=1) if end_of_day else day
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"Cannot read the date {value!r}. Use YYYY-MM-DD, an ISO 8601 date-time, today, yesterday, or 7d / 12h / 2w."
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ref.tzinfo)
    return parsed
