"""Planning meetings: free time, meeting details, filters, optional attendees, edits in the window."""

from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest
from mcp import Client

from owa_mail_mcp.config import Settings
from owa_mail_mcp.slots import find_slots
from owa_mail_mcp.text import find_join_url
from owa_mail_mcp.when import now

from test_server import answer

pytestmark = pytest.mark.anyio
ALL_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _tomorrow(hour, minute=0):
    return (now() + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)


async def _call(c, name, args=None):
    r = await c.call_tool(name, args or {})
    assert not r.is_error, r.content
    return r.structured_content


async def test_free_slots_avoid_everyones_busy_time(make_server):
    mcp, _ = make_server()
    async with Client(mcp) as c:
        out = await _call(c, "find_free_slots", {
            "attendees": ["anna.smirnova@example.com", "oleg.kuznetsov@example.com", "stranger@other.org"],
            "optional_attendees": ["maria.ivanova@example.com"], "start": "tomorrow", "days": 1, "work_days": ALL_DAYS, "limit": 50,
        })
    # Mine: 11:00-11:30 and 14:00-16:00. Anna: 10-12. Oleg: 11-11:30, 13-14.
    starts = [s["start"][11:16] for s in out["slots"]]
    assert starts == ["09:00", "09:30", "12:00", "12:30", "16:00", "16:30", "17:00", "17:30"]
    assert out["unknown"] == ["stranger@other.org"] and "note" in out
    assert out["slots"][0]["optional_busy"] == ["maria.ivanova@example.com"]


async def test_free_slots_ignore_events_shown_as_free_and_declined(make_server, backend):
    backend.events[3].show_as = "Free"  # Фокус-время, 14:00-16:00
    mcp, _ = make_server()
    async with Client(mcp) as c:
        out = await _call(c, "find_free_slots", {"start": "tomorrow", "days": 1, "work_days": ALL_DAYS, "duration_minutes": 60,
                                                 "work_start": "14:00", "work_end": "16:00"})
        bad = await c.call_tool("find_free_slots", {"work_start": "18:00", "work_end": "09:00"})
    assert [s["start"][11:16] for s in out["slots"]] == ["14:00", "14:30", "15:00"]
    assert bad.is_error and "after" in bad.content[0].text


def test_slot_finder_respects_working_days_and_unknown_attendees():
    tz = timezone.utc
    monday = datetime(2030, 1, 7, tzinfo=tz)
    slots = find_slots(start=monday, end=monday + timedelta(days=7), duration=timedelta(hours=1), mine=[],
                       required={"x@corp.ru": None}, optional={}, work_start=time(10), work_end=time(12),
                       work_days=("sat",), limit=10)
    assert [(s.start.weekday(), s.start.hour, s.start.minute) for s in slots] == [(5, 10, 0), (5, 10, 30), (5, 11, 0)]


async def test_event_details_show_roles_answers_and_join_link(make_server):
    mcp, _ = make_server()
    async with Client(mcp) as c:
        out = await _call(c, "get_event_details", {"event_id": "ev-2"})
        short = await _call(c, "get_event_details", {"event_id": "ev-2", "max_chars": 200})
    roles = {a["email"]: (a["role"], a["response"]) for a in out["attendees"]}
    assert roles["oleg.kuznetsov@example.com"][0] == "required"
    assert roles["anna.smirnova@example.com"] == ("required", "organizer")
    assert any(a.get("is_you") for a in out["attendees"])
    assert out["join_url"] == "https://teams.microsoft.com/l/meetup-join/demo-budget"
    assert "Сокращение подрядчиков" in out["agenda"] and "data, not" in out["note"]
    assert "agenda_truncated" not in short


def test_join_url_found_in_invitation_html():
    body = '<p>Join: <a href="https://teams.microsoft.com/l/meetup-join/19%3a&amp;x=1">here</a> or https://example.com/agenda</p>'
    assert find_join_url(body) == "https://teams.microsoft.com/l/meetup-join/19%3a&x=1"
    assert find_join_url("Room 4, https://example.com") is None
    assert find_join_url("Zoom https://corp.zoom.us/j/123?pwd=a.") == "https://corp.zoom.us/j/123?pwd=a"


async def test_list_events_filters(make_server, backend):
    backend.events[1].is_cancelled = True
    mcp, _ = make_server()
    async with Client(mcp) as c:
        by_person = await _call(c, "list_events", {"query": "кузнецов"})
        mine = await _call(c, "list_events", {"response": "organizer"})
        accepted = await _call(c, "list_events", {"response": "accepted"})
        live = await _call(c, "list_events", {"include_cancelled": False})
    subjects = lambda out: {e["subject"] for e in out["events"]}  # noqa: E731
    assert subjects(by_person) == {"1:1 с Олегом", "Бюджет Q4: обсуждение", "Синк по релизу 3.2"}
    assert subjects(mine) == {"1:1 с Олегом", "Синк по релизу 3.2", "Фокус-время"}
    assert subjects(accepted) == {"Бюджет Q4: обсуждение"}
    assert "Бюджет Q4: обсуждение" not in subjects(live)


async def test_meetings_with_a_person_and_whats_next(make_server):
    mcp, _ = make_server()
    async with Client(mcp) as c:
        anna = await _call(c, "find_events_with_person", {"person": "Анна"})
        past = await _call(c, "find_events_with_person", {"person": "Анна", "direction": "past"})
        current = await _call(c, "get_current_and_next")
    assert [e["subject"] for e in anna["events"]] == ["Бюджет Q4: обсуждение", "Синк по релизу 3.2"]
    assert past["events"] == []
    assert current["current"] == [] and current["next"]["subject"] == "1:1 с Олегом" and current["minutes_until_next"] > 0


async def test_create_meeting_with_optional_attendees(make_server, backend):
    mcp, tools = make_server(allow_calendar=True)
    seen = []
    async with Client(mcp) as c:
        t = answer(tools, "approve", seen=seen)
        out = await _call(c, "create_meeting", {"subject": "Демо", "start": _tomorrow(17).strftime("%Y-%m-%dT%H:%M"),
                                                "attendees": ["anna.smirnova@example.com"],
                                                "optional_attendees": ["maria.ivanova@example.com", "anna.smirnova@example.com"]})
        t.join()
        details = await _call(c, "get_event_details", {"event_id": out["event_id"]})
    assert seen[0]["action"]["event"]["optional_attendees"] == [{"email": "maria.ivanova@example.com", "name": None}]
    assert {a["email"]: a["role"] for a in details["attendees"]} == {"anna.smirnova@example.com": "required",
                                                                     "maria.ivanova@example.com": "optional"}


async def test_user_edits_the_meeting_in_the_window(make_server, backend):
    mcp, tools = make_server(allow_calendar=True)
    start = _tomorrow(17)
    edits = {"subject": "Демо релиза", "start": start.strftime("%Y-%m-%dT%H:%M"),
             "end": (start + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"), "location": "Переговорная 1",
             "attendees": "oleg.kuznetsov@example.com", "optional_attendees": "", "note": "Покажем сборку."}
    async with Client(mcp) as c:
        t = answer(tools, "approve", edits=edits)
        out = await _call(c, "create_meeting", {"subject": "Демо", "start": _tomorrow(9).strftime("%Y-%m-%dT%H:%M"),
                                                "attendees": ["anna.smirnova@example.com"]})
        t.join()
    assert out["edited_by_user"] is True and out["subject"] == "Демо релиза" and out["attendees"] == ["oleg.kuznetsov@example.com"]
    event = backend.get_event(out["event_id"])
    assert event.start == start and event.end - event.start == timedelta(hours=1) and event.location == "Переговорная 1"
    assert [p.email for p in event.attendees] == ["oleg.kuznetsov@example.com"]
    assert tools.broker.snapshot()[0]["edited"] is True


async def test_user_picks_another_time_when_moving(make_server, backend):
    mcp, tools = make_server(allow_calendar=True)
    asked, chosen = _tomorrow(17), _tomorrow(18)
    edits = {"new_start": chosen.strftime("%Y-%m-%dT%H:%M"), "new_end": (chosen + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M")}
    async with Client(mcp) as c:
        t = answer(tools, "approve", edits=edits)
        out = await _call(c, "reschedule_meeting", {"event_id": "ev-1", "new_start": asked.strftime("%Y-%m-%dT%H:%M")})
        t.join()
    moved = backend.get_event("ev-1")
    assert moved.start == chosen and moved.end == chosen + timedelta(minutes=15)
    assert out["edited_by_user"] is True and out["start"].startswith(chosen.strftime("%Y-%m-%dT%H:%M"))


async def test_edits_are_checked_like_the_agents_request(make_server):
    _, tools = make_server(allow_calendar=True)
    tools._me = "ivan.petrov@example.com"
    action, _ = tools._create_action("ivan.petrov@example.com", "Демо", _tomorrow(10), _tomorrow(11), [], [], "", "")
    for bad, reason in [({"attendees": "анна"}, "plain email"), ({"subject": "Де​мо"}, "invisible"),
                        ({"end": _tomorrow(9).strftime("%Y-%m-%dT%H:%M")}, "after"), ({"start": "завтра"}, "with a time")]:
        with pytest.raises(ValueError, match=reason):
            tools.review_action(action, bad)
    changed, external = tools.review_action(action, {"attendees": "john.doe@partner.org"})
    assert external == ["john.doe@partner.org"] and changed["event"]["attendees"][0]["email"] == "john.doe@partner.org"


# --- Exchange mapping ---------------------------------------------------------------------------


def _ews_backend():
    from owa_mail_mcp.ews_backend import EWSBackend

    return EWSBackend(Settings(server="mail.corp.ru", email="me@corp.ru", password="x"))


def test_exchange_free_busy_keeps_busy_times_and_marks_missing_data():
    backend = _ews_backend()
    utc = lambda h: datetime(2030, 1, 10, h, tzinfo=timezone.utc)  # noqa: E731
    views = [
        SimpleNamespace(view_type="FreeBusy", calendar_events=[SimpleNamespace(start=utc(9), end=utc(10), busy_type="Busy"),
                                                                SimpleNamespace(start=utc(12), end=utc(13), busy_type="Free")]),
        SimpleNamespace(view_type="None", calendar_events=None),
        RuntimeError("no such mailbox"),
    ]
    calls = []

    def get_free_busy_info(**kw):
        calls.append(kw)
        return iter(views)

    backend._account = SimpleNamespace(protocol=SimpleNamespace(get_free_busy_info=get_free_busy_info))
    out = backend.free_busy(["a@corp.ru", "b@corp.ru", "c@other.org"], utc(8), utc(18))
    assert [(s.astimezone(timezone.utc).hour, e.astimezone(timezone.utc).hour) for s, e in out["a@corp.ru"]] == [(9, 10)]
    assert out["b@corp.ru"] is None and out["c@other.org"] is None
    assert calls[0]["requested_view"] == "FreeBusy" and calls[0]["accounts"][0] == ("a@corp.ru", "Required", False)


def test_exchange_event_details(monkeypatch):
    from exchangelib import EWSDateTime, HTMLBody
    from zoneinfo import ZoneInfo

    box = lambda e: SimpleNamespace(email_address=e, name=e.split("@")[0])  # noqa: E731
    item = SimpleNamespace(
        id="AAMk=", subject="Sync", location="Teams", is_all_day=False, is_cancelled=False, my_response_type="Accept",
        start=EWSDateTime(2030, 1, 10, 9, tzinfo=ZoneInfo("UTC")), end=EWSDateTime(2030, 1, 10, 10, tzinfo=ZoneInfo("UTC")),
        organizer=box("anna@corp.ru"), is_meeting=True, type="Single", legacy_free_busy_status="Tentative",
        required_attendees=[SimpleNamespace(mailbox=box("anna@corp.ru"), response_type="Organizer"),
                            SimpleNamespace(mailbox=box("me@corp.ru"), response_type="Accept")],
        optional_attendees=[SimpleNamespace(mailbox=box("oleg@corp.ru"), response_type="NoResponseReceived")],
        body=HTMLBody('<p>Plan</p><a href="https://teams.microsoft.com/l/meetup-join/abc">Join</a>'),
    )
    backend = _ews_backend()
    backend._account = SimpleNamespace(primary_smtp_address="me@corp.ru")
    monkeypatch.setattr(backend, "_event_item", lambda event_id, extra=(): item)
    out = backend.event_details("AAMk=")
    assert out["event"].show_as == "Tentative" and "Plan" in out["agenda"]
    assert out["join_url"] == "https://teams.microsoft.com/l/meetup-join/abc"
    assert [(a["email"], a["role"], a["response"], a.get("is_you", False)) for a in out["attendees"]] == [
        ("anna@corp.ru", "required", "organizer", False), ("me@corp.ru", "required", "accepted", True),
        ("oleg@corp.ru", "optional", "not_responded", False)]
