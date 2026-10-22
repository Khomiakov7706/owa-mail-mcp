"""Free time for a meeting: the user's own calendar and the attendees' free/busy, inside working hours."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

BUSY = {"Busy", "Tentative", "OOF"}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
Interval = tuple[datetime, datetime]


@dataclass
class Slot:
    start: datetime
    end: datetime
    optional_free: list[str] = field(default_factory=list)
    optional_busy: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        out = {"start": self.start.isoformat(timespec="minutes"), "end": self.end.isoformat(timespec="minutes")}
        if self.optional_free or self.optional_busy:
            out["optional_free"] = self.optional_free
            out["optional_busy"] = self.optional_busy
        return out


def _overlaps(start: datetime, end: datetime, busy: list[Interval]) -> bool:
    return any(b_start < end and b_end > start for b_start, b_end in busy)


def find_slots(
    *,
    start: datetime,
    end: datetime,
    duration: timedelta,
    mine: list[Interval],
    required: dict[str, list[Interval] | None],
    optional: dict[str, list[Interval] | None],
    work_start: time,
    work_end: time,
    work_days: tuple[str, ...],
    limit: int,
) -> list[Slot]:
    """Slots on the hour or half hour when the user and every required attendee whose free/busy is known are free.
    A required attendee without data (None) does not block a slot; the caller reports them as unknown."""
    tz = start.tzinfo
    out: list[Slot] = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < end and len(out) < limit:
        if DAYS[day.weekday()] in work_days:
            t = max(day.replace(hour=work_start.hour, minute=work_start.minute), start)
            # Round up to the next :00 or :30.
            if t.minute % 30 or t.second or t.microsecond:
                t = t.replace(second=0, microsecond=0) + timedelta(minutes=30 - t.minute % 30)
            day_end = min(day.replace(hour=work_end.hour, minute=work_end.minute), end)
            while t + duration <= day_end and len(out) < limit:
                slot_end = t + duration
                blocked = _overlaps(t, slot_end, mine) or any(
                    busy is not None and _overlaps(t, slot_end, busy) for busy in required.values()
                )
                if not blocked:
                    free = [who for who, busy in optional.items() if busy is not None and not _overlaps(t, slot_end, busy)]
                    taken = [who for who, busy in optional.items() if busy is not None and _overlaps(t, slot_end, busy)]
                    out.append(Slot(t, slot_end, free, taken))
                t += timedelta(minutes=30)
        day = (day + timedelta(days=1)).replace(tzinfo=tz)
    return out


def parse_hhmm(value: str, name: str) -> time:
    try:
        hours, minutes = value.split(":")
        return time(int(hours), int(minutes))
    except (ValueError, AttributeError):
        raise ValueError(f"`{name}` must look like 09:00.") from None
