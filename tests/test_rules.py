"""Pausing the countdown, and trusted-recipient rules for one agent."""

import threading
import time

import pytest
from mcp import Client
from mcp.types import Implementation

from owa_mail_mcp.config import Settings, normalize_rules
from owa_mail_mcp.confirm import PAUSE_LIMIT, ConfirmationBroker, Status
from owa_mail_mcp.models import Outgoing
from owa_mail_mcp.safety import OutgoingReview
from owa_mail_mcp.server import matching_rule
from owa_mail_mcp.setup import parse_rules

from test_server import _request, answer

pytestmark = pytest.mark.anyio
REPORT = {"agent": "OpenCode", "recipients": ["oleg.kuznetsov@example.com", "anna.smirnova@example.com"]}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _broker(clock):
    broker = ConfirmationBroker(lambda m: OutgoingReview(), clock=clock)
    p = broker.submit(Outgoing(to=["a@b.ru"], subject="s", body="b"), OutgoingReview(), client="c", sender="me", timeout=50)
    return broker, p.id


def test_paused_question_does_not_expire_and_resumes_with_its_time():
    clock = Clock()
    broker, pid = _broker(clock)
    clock.now += 20
    assert broker.hold(pid, True) is None
    clock.now += 600
    item = broker.snapshot()[0]
    assert item["status"] == "pending" and item["paused"] is True and item["seconds_left"] == 30
    broker.hold(pid, False)
    clock.now += 29
    assert broker.snapshot()[0]["status"] == "pending"
    clock.now += 2
    assert broker.wait(pid, 0) == Status.EXPIRED


def test_a_forgotten_pause_still_ends():
    clock = Clock()
    broker, pid = _broker(clock)
    broker.hold(pid, True)
    clock.now += PAUSE_LIMIT + 1
    assert broker.wait(pid, 0) == Status.EXPIRED
    assert broker.hold(pid, False).code == "already"


async def test_window_pause_button_holds_the_agent(make_server, backend):
    mcp, tools = make_server(confirm_timeout=2)

    def user():
        for _ in range(100):
            items = [i for i in _request(tools.ui, "api/pending")[1]["items"] if i["status"] == "pending"]
            if items:
                _request(tools.ui, "api/hold", {"id": items[0]["id"], "paused": True})
                time.sleep(3)  # longer than the timeout
                _request(tools.ui, "api/decide", {"id": items[0]["id"], "action": "approve"})
                return
            time.sleep(0.05)

    t = threading.Thread(target=user, daemon=True)
    t.start()
    async with Client(mcp) as c:
        r = await c.call_tool("send_email", {"to": ["anna.smirnova@example.com"], "subject": "Пауза", "body": "Текст"})
    t.join()
    assert not r.is_error, r.content
    assert r.structured_content["sent"] is True


def _msg(to, subject="Еженедельный отчёт: неделя 46"):
    return Outgoing(to=to, subject=subject, body="Отчёт")


def test_rule_covers_only_its_agent_and_recipients():
    s = Settings(trusted_rules=normalize_rules([REPORT]), trusted_recipients=("maria.ivanova@example.com",))
    assert matching_rule(s, _msg(["oleg.kuznetsov@example.com"]), "OpenCode") == {"agent": "OpenCode"}
    assert matching_rule(s, _msg(["oleg.kuznetsov@example.com"]), "opencode") is not None  # names compare case-insensitively
    assert matching_rule(s, _msg(["oleg.kuznetsov@example.com"]), "Cursor") is None
    assert matching_rule(s, _msg(["oleg.kuznetsov@example.com"], "Привет"), "OpenCode") is not None  # any subject
    assert matching_rule(s, _msg(["oleg.kuznetsov@example.com", "john.doe@partner.org"]), "OpenCode") is None
    assert matching_rule(s, _msg(["maria.ivanova@example.com"], "Что угодно"), "Cursor") == {"agent": "any"}


def test_setup_checks_rules():
    rules, error = parse_rules([{"agent": " OpenCode ", "recipients": "oleg@corp.ru\nanna@corp.ru"}, {"agent": "", "recipients": ""}])
    assert error is None and rules == ({"agent": "OpenCode", "recipients": ["oleg@corp.ru", "anna@corp.ru"]},)
    assert "1" in parse_rules([{"agent": "", "recipients": "a@b.ru"}])[1]
    assert parse_rules([{"agent": "X", "recipients": "олег"}])[1]
    assert parse_rules(None) == (None, None)


async def test_report_goes_out_without_the_window_only_from_that_agent(make_server, backend):
    mcp, tools = make_server(trusted_rules=normalize_rules([REPORT]))
    seen = []
    async with Client(mcp, client_info=Implementation(name="OpenCode", version="1")) as c:
        status = (await c.call_tool("get_status", {})).structured_content
        ok = await c.call_tool("send_email", {"to": ["oleg.kuznetsov@example.com"], "subject": "Еженедельный отчёт: неделя 46",
                                              "body": "Сделано: …"})
        t = answer(tools, "reject", seen=seen)
        outsider = await c.call_tool("send_email", {"to": ["oleg.kuznetsov@example.com", "maria.ivanova@example.com"],
                                                    "subject": "Обед?", "body": "Пойдём?"})
        t.join()
    async with Client(mcp, client_info=Implementation(name="Cursor", version="1")) as c:
        t = answer(tools, "reject")
        other_agent = await c.call_tool("send_email", {"to": ["oleg.kuznetsov@example.com"], "subject": "Еженедельный отчёт: неделя 47",
                                                       "body": "Сделано: …"})
        t.join()
    assert status["trusted_rules_for_you"] == [{"recipients": REPORT["recipients"]}]
    assert ok.structured_content["confirmed"] == "trusted_recipients"
    assert ok.structured_content["trusted_rule"] == {"agent": "OpenCode"}
    assert outsider.is_error and seen  # a recipient outside the rule: the window
    assert other_agent.is_error
    history = [i for i in tools.broker.snapshot() if i["auto"]]
    assert history[0]["rule"]["agent"] == "OpenCode"


def test_rules_are_signed_like_other_permissions(tmp_path, monkeypatch):
    import json

    from owa_mail_mcp import config

    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.json")
    config.save_settings(Settings(server="mail.corp.ru", email="me@corp.ru", trusted_rules=normalize_rules([REPORT])))
    assert Settings.load(with_password=False).trusted_rules[0]["agent"] == "OpenCode"
    raw = json.loads((tmp_path / "config.json").read_text())
    raw["trusted_rules"][0]["recipients"].append("attacker@evil.test")
    (tmp_path / "config.json").write_text(json.dumps(raw))
    loaded = Settings.load(with_password=False)
    assert loaded.tampered and loaded.trusted_rules == ()
