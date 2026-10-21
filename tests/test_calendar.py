from datetime import timedelta

import pytest
from mcp import Client

from owa_mail_mcp.config import Settings
from owa_mail_mcp.server import build_server
from owa_mail_mcp.when import now

from test_server import answer

pytestmark = pytest.mark.anyio


async def _events(c, **args):
    r = await c.call_tool("list_events", {"start": "today", "days": 7, **args})
    assert not r.is_error, r.content
    return {e["subject"]: e for e in r.structured_content["events"]}


async def test_list_events_shows_who_organizes(make_server):
    mcp, _ = make_server(allow_calendar=True)
    async with Client(mcp) as c:
        events = await _events(c)
    one = events["1:1 с Олегом"]
    assert one["i_am_organizer"] is True and one["attendees"][0]["email"] == "oleg.kuznetsov@example.com"
    assert events["Бюджет Q4: обсуждение"]["i_am_organizer"] is False
    assert events["Синк по релизу 3.2"]["repeats"] is True
    assert list(events) == sorted(events, key=lambda s: events[s]["start"])


async def test_changes_refused_unless_enabled(backend):
    mcp, _ = build_server(Settings(backend="demo", allow_calendar=False), backend)
    async with Client(mcp) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        r = await c.call_tool("cancel_meeting", {"event_id": "ev-1"})
    assert {"list_events", "cancel_meeting", "reschedule_meeting"} <= names
    assert r.is_error and "turned off" in r.content[0].text
    assert backend.calendar_log == []


async def test_cancel_own_meeting_after_user_approves(make_server, backend):
    mcp, tools = make_server(allow_calendar=True)
    seen = []
    async with Client(mcp) as c:
        t = answer(tools, "approve", seen=seen)
        r = await c.call_tool("cancel_meeting", {"event_id": "ev-1", "message": "Перенесём на следующую неделю."})
        t.join()
        events = await _events(c)
    assert not r.is_error, r.content
    assert r.structured_content == {"done": True, "outcome": "cancelled", "subject": "1:1 с Олегом", "attendees_notified": True}
    card = seen[0]
    assert card["message"] is None and card["action"]["kind"] == "cancel" and card["action"]["note"] == "Перенесём на следующую неделю."
    assert backend.calendar_log == [("cancelled", "ev-1")] and "1:1 с Олегом" not in events
    history = tools.broker.snapshot()[0]
    assert history["status"] == "sent" and history["action"]["kind"] == "cancel"


async def test_someone_elses_meeting_is_declined_not_cancelled(make_server, backend):
    mcp, tools = make_server(allow_calendar=True)
    seen = []
    async with Client(mcp) as c:
        t = answer(tools, "approve", seen=seen)
        r = await c.call_tool("cancel_meeting", {"event_id": "ev-2"})
        t.join()
    assert r.structured_content["outcome"] == "declined" and seen[0]["action"]["kind"] == "decline"


async def test_nothing_changes_when_the_user_says_no(make_server, backend):
    mcp, tools = make_server(allow_calendar=True)
    async with Client(mcp) as c:
        t = answer(tools, "reject")
        r = await c.call_tool("cancel_meeting", {"event_id": "ev-1"})
        t.join()
    assert r.is_error and "Nothing was changed" in r.content[0].text
    assert backend.calendar_log == []


async def test_reschedule_keeps_length_and_shows_both_times(make_server, backend):
    mcp, tools = make_server(allow_calendar=True)
    target = (now() + timedelta(days=4)).replace(hour=16, minute=0, second=0, microsecond=0)
    seen = []
    async with Client(mcp) as c:
        t = answer(tools, "approve", seen=seen)
        r = await c.call_tool("reschedule_meeting", {"event_id": "ev-1", "new_start": target.strftime("%Y-%m-%dT%H:%M")})
        t.join()
    assert not r.is_error, r.content
    moved = backend.get_event("ev-1")
    assert moved.start == target and moved.end - moved.start == timedelta(minutes=30)
    action = seen[0]["action"]
    assert action["kind"] == "reschedule" and action["new_start"].startswith(target.strftime("%Y-%m-%dT%H:%M"))
    assert action["event"]["start"] != action["new_start"]


async def test_reschedule_checks_before_asking(make_server, backend):
    mcp, tools = make_server(allow_calendar=True)
    async with Client(mcp) as c:
        someone_elses = await c.call_tool("reschedule_meeting", {"event_id": "ev-2", "new_start": "2030-01-10T10:00"})
        no_time = await c.call_tool("reschedule_meeting", {"event_id": "ev-1", "new_start": "2030-01-10"})
        past = await c.call_tool("reschedule_meeting", {"event_id": "ev-1", "new_start": "2020-01-10T10:00"})
        backwards = await c.call_tool("reschedule_meeting", {"event_id": "ev-1", "new_start": "2030-01-10T10:00", "new_end": "2030-01-10T09:00"})
        missing = await c.call_tool("cancel_meeting", {"event_id": "nope"})
    assert someone_elses.is_error and "Only the organizer (Анна Смирнова)" in someone_elses.content[0].text
    assert no_time.is_error and "with a time" in no_time.content[0].text
    assert past.is_error and "past" in past.content[0].text
    assert backwards.is_error and "after" in backwards.content[0].text
    assert missing.is_error and "list_events" in missing.content[0].text
    assert tools.broker.snapshot() == [] and backend.calendar_log == []


def test_event_fields_exist_in_exchangelib():
    from exchangelib import CalendarItem

    from owa_mail_mcp.ews_backend import _EVENT_FIELDS

    names = {f.name for f in CalendarItem.FIELDS} | {"id", "changekey"}
    assert set(_EVENT_FIELDS) <= names


class _Item:
    """Stands in for an exchangelib CalendarItem: records what the backend asks Exchange to do."""

    def __init__(self, organizer, attendees, response, is_meeting=True, kind="Single"):
        from types import SimpleNamespace

        from exchangelib import EWSDateTime
        from zoneinfo import ZoneInfo

        box = lambda e: SimpleNamespace(email_address=e, name=e.split("@")[0])  # noqa: E731
        self.id, self.subject, self.location, self.is_all_day, self.is_cancelled = "AAMk=", "Sync", "", False, False
        self.start = EWSDateTime(2030, 1, 10, 9, 0, tzinfo=ZoneInfo("UTC"))
        self.end = EWSDateTime(2030, 1, 10, 9, 30, tzinfo=ZoneInfo("UTC"))
        self.organizer = box(organizer)
        self.required_attendees = [SimpleNamespace(mailbox=box(a)) for a in attendees]
        self.optional_attendees = None
        self.my_response_type, self.is_meeting, self.type = response, is_meeting, kind
        self.calls = []

    def cancel(self, **kw):
        self.calls.append(("cancel", kw))

    def decline(self, **kw):
        self.calls.append(("decline", kw))

    def delete(self, **kw):
        self.calls.append(("delete", kw))

    def save(self, **kw):
        self.calls.append(("save", kw))


def _ews(monkeypatch, item):
    from types import SimpleNamespace

    from owa_mail_mcp.ews_backend import EWSBackend

    backend = EWSBackend(Settings(server="mail.corp.ru", email="me@corp.ru", password="x"))
    backend._account = SimpleNamespace(primary_smtp_address="me@corp.ru")
    monkeypatch.setattr(backend, "_event_item", lambda event_id: item)
    return backend


@pytest.mark.parametrize("organizer,response,is_meeting,attendees,expected", [
    ("me@corp.ru", "Organizer", True, ["me@corp.ru", "anna@corp.ru"], "cancelled"),
    ("anna@corp.ru", "Accept", True, ["me@corp.ru", "anna@corp.ru"], "declined"),
    ("me@corp.ru", "Organizer", False, [], "deleted"),
    ("me@corp.ru", "Organizer", True, ["me@corp.ru"], "deleted"),  # a "meeting" with nobody else is an appointment
])
def test_exchange_cancel_picks_the_right_operation(monkeypatch, organizer, response, is_meeting, attendees, expected):
    item = _Item(organizer, attendees, response, is_meeting)
    backend = _ews(monkeypatch, item)
    assert backend.cancel_event("AAMk=", "Sorry") == expected
    op, kwargs = item.calls[0]
    assert op == {"cancelled": "cancel", "declined": "decline", "deleted": "delete"}[expected]
    if op == "cancel":
        assert str(kwargs["new_body"]) == "Sorry"
    if op == "delete":
        assert kwargs["send_meeting_cancellations"] == "SendToNone"


def test_exchange_reschedule_sends_updates_only_for_meetings(monkeypatch):
    from datetime import datetime, timezone

    meeting = _Item("me@corp.ru", ["anna@corp.ru"], "Organizer", kind="Occurrence")
    backend = _ews(monkeypatch, meeting)
    event = backend.get_event("AAMk=")
    assert event.kind == "occurrence" and event.is_organizer and [p.email for p in event.attendees] == ["anna@corp.ru"]
    new = datetime(2030, 1, 11, 12, 0, tzinfo=timezone.utc)
    backend.reschedule_event("AAMk=", new, new + timedelta(minutes=30))
    op, kwargs = meeting.calls[0]
    assert op == "save" and kwargs == {"update_fields": ["start", "end"], "send_meeting_invitations": "SendToAllAndSaveCopy"}
    assert meeting.start.hour == 12

    invited = _Item("anna@corp.ru", ["me@corp.ru", "anna@corp.ru"], "Accept")
    with pytest.raises(Exception, match="Only the organizer"):
        _ews(monkeypatch, invited).reschedule_event("AAMk=", new, new + timedelta(minutes=30))
    assert invited.calls == []
