import json
import threading
import time
import urllib.error
import urllib.request

import pytest
from mcp import Client

from owa_mail_mcp.config import Settings
from owa_mail_mcp.demo_backend import DemoBackend
from owa_mail_mcp.server import build_server

pytestmark = pytest.mark.anyio


def _request(ui, path, payload=None, headers=None, url=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url or ui.url + path, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, body


def answer(tools, action, edits=None, seen=None):
    """Plays the user: waits for the card to appear in the window, then presses a button."""
    def run():
        for _ in range(100):
            _, data = _request(tools.ui, "api/pending")
            pending = [i for i in data["items"] if i["status"] == "pending"]
            if pending:
                if seen is not None:
                    seen.append(pending[0])
                payload = {"id": pending[0]["id"], "action": action}
                if edits:
                    payload["edits"] = edits
                _request(tools.ui, "api/decide", payload)
                return
            time.sleep(0.05)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


async def test_read_tools(make_server):
    mcp, _ = make_server()
    async with Client(mcp) as c:
        status = (await c.call_tool("get_status", {})).structured_content
        assert status["me"]["email"] == "ivan.petrov@example.com" and status["unread_in_inbox"] == 4

        new = (await c.call_tool("check_mail", {})).structured_content
        assert new["count"] == 4 and all(not m["is_read"] for m in new["messages"])

        found = (await c.call_tool("search_messages", {"sender": "Олег", "folder": "all"})).structured_content
        assert {m["subject"] for m in found["messages"]} == {"RE: Бюджет Q4: черновик", "Инцидент в проде: разбор"}

        thread = (await c.call_tool("get_thread", {"message_id": "demo-4"})).structured_content
        assert thread["message_count"] == 4
        assert [m["message_id"] for m in thread["messages"]] == ["demo-1", "demo-2", "demo-3", "demo-4"]
        assert thread["messages"][1]["body"] == "Анна, спасибо. Подрядчиков сократим на 10%, остальное ок."
        assert thread["messages"][1]["quoted_history_removed"] is True

        senders = (await c.call_tool("list_senders", {"since": "30d"})).structured_content
        assert senders["senders"][0]["person"]["email"] == "anna.smirnova@example.com"
        assert senders["senders"][0]["messages"] == 2

        people = (await c.call_tool("find_people", {"queries": ["кузнецов", "ivan.petrov@example.com"]})).structured_content
        assert people["results"][0]["people"][0]["email"] == "oleg.kuznetsov@example.com"
        assert people["results"][1]["people"][0]["is_me"] is True


async def test_bad_input_is_a_tool_error(make_server):
    mcp, _ = make_server()
    async with Client(mcp) as c:
        r = await c.call_tool("get_message", {"message_id": "nope"})
        assert r.is_error and "No message" in r.content[0].text
        r = await c.call_tool("search_messages", {"since": "next tuesday"})
        assert r.is_error and "Cannot read the date" in r.content[0].text


async def test_send_refused_unless_enabled(backend):
    mcp, _ = build_server(Settings(backend="demo", allow_send=False), backend)
    async with Client(mcp) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        r = await c.call_tool("send_email", {"to": ["anna.smirnova@example.com"], "subject": "Q4", "body": "Ок"})
    assert "send_email" in names
    assert r.is_error and "turned off" in r.content[0].text
    assert backend.sent == []


async def test_send_after_user_approves(make_server, backend):
    mcp, tools = make_server()
    seen = []
    async with Client(mcp) as c:
        t = answer(tools, "approve", seen=seen)
        r = await c.call_tool("send_email", {"to": ["anna.smirnova@example.com", "john.doe@partner.org"], "subject": "Q4", "body": "Ок"})
        t.join()
    assert not r.is_error, r.content
    assert r.structured_content["sent"] is True and r.structured_content["edited_by_user"] is False
    assert seen[0]["external"] == ["john.doe@partner.org"]
    assert backend.sent[0].to == ["anna.smirnova@example.com", "john.doe@partner.org"]


async def test_reply_fills_recipients_and_subject(make_server, backend):
    mcp, tools = make_server()
    seen = []
    async with Client(mcp) as c:
        t = answer(tools, "approve", seen=seen)
        r = await c.call_tool("send_email", {"to": [], "body": "Оценку пришлю завтра.", "reply_to_message_id": "demo-4", "reply_all": True})
        t.join()
    assert r.structured_content["sent"] is True
    sent = backend.sent[0]
    assert sent.to == ["anna.smirnova@example.com"] and sent.cc == ["oleg.kuznetsov@example.com"]
    assert sent.subject == "RE: Бюджет Q4: черновик" and sent.reply_to_id == "demo-4"
    assert seen[0]["reply_context"]["from"] == "Анна Смирнова"


async def test_user_edits_are_what_gets_sent(make_server, backend):
    mcp, tools = make_server()
    async with Client(mcp) as c:
        t = answer(tools, "approve", edits={"to": "oleg.kuznetsov@example.com", "subject": "Другая тема", "body": "Исправленный текст"})
        r = await c.call_tool("send_email", {"to": ["anna.smirnova@example.com"], "subject": "Q4", "body": "Ок"})
        t.join()
    assert r.structured_content["edited_by_user"] is True
    assert r.structured_content["body"] == "Исправленный текст"
    assert backend.sent[0].to == ["oleg.kuznetsov@example.com"] and backend.sent[0].subject == "Другая тема"


async def test_rejected_and_expired_send_nothing(make_server, backend):
    mcp, tools = make_server(confirm_timeout=1)
    async with Client(mcp) as c:
        t = answer(tools, "reject")
        r = await c.call_tool("send_email", {"to": ["anna.smirnova@example.com"], "subject": "Q4", "body": "Ок"})
        t.join()
        assert r.is_error and "declined" in r.content[0].text
        r = await c.call_tool("send_email", {"to": ["anna.smirnova@example.com"], "subject": "Q4", "body": "Ок 2"})
        assert r.is_error and "did not confirm" in r.content[0].text
    assert backend.sent == []


async def test_policy_refusal_never_reaches_the_window(make_server, backend):
    mcp, tools = make_server()
    async with Client(mcp) as c:
        r = await c.call_tool("send_email", {"to": ["ivan@еxample.com"], "subject": "x", "body": "y"})
    assert r.is_error and "plain email" in r.content[0].text
    assert tools.broker.snapshot() == []


async def test_duplicate_call_returns_first_result(make_server, backend):
    mcp, tools = make_server()
    args = {"to": ["anna.smirnova@example.com"], "subject": "Q4", "body": "Ок"}
    async with Client(mcp) as c:
        t = answer(tools, "approve")
        await c.call_tool("send_email", args)
        t.join()
        r = await c.call_tool("send_email", args)
    assert r.structured_content["duplicate"] is True and len(backend.sent) == 1


async def test_window_refuses_foreign_requests(make_server):
    _, tools = make_server()
    ui = tools.ui
    base = f"http://127.0.0.1:{ui.port}"
    assert _request(ui, "", url=f"{base}/t/wrong-token/api/pending")[0] == 404
    assert _request(ui, "", url=ui.url + "api/pending", headers={"Host": f"evil.example:{ui.port}"})[0] == 404
    assert _request(ui, "api/decide", {"id": "x", "action": "approve"}, headers={"Origin": "https://evil.example"})[0] == 403
    req = urllib.request.Request(ui.url + "api/decide", data=b"id=x&action=approve", method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 415
    with urllib.request.urlopen(ui.url, timeout=5) as resp:
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]


async def test_auth_failure_latches(backend):
    from owa_mail_mcp.backend import AuthenticationError

    calls = []

    def whoami():
        calls.append(1)
        raise AuthenticationError("Exchange rejected the credentials.")

    backend.whoami = whoami
    mcp, _ = build_server(Settings(backend="demo"), backend)
    async with Client(mcp) as c:
        r1 = await c.call_tool("get_status", {})
        r2 = await c.call_tool("check_mail", {})
    assert r1.is_error and r2.is_error and "rejected" in r2.content[0].text
    assert len(calls) == 1


async def test_errors_say_when_the_server_runs_old_code(backend, monkeypatch):
    from owa_mail_mcp import runtime
    from owa_mail_mcp.backend import BackendError

    def broken(*args, **kwargs):
        raise BackendError("Cannot reach Exchange.")

    def surprise(*args, **kwargs):
        raise RuntimeError("odd server answer")

    backend.unread_count = broken
    backend.search = surprise
    mcp, _ = build_server(Settings(backend="demo"), backend)
    async with Client(mcp) as c:
        r = await c.call_tool("check_mail", {})
        assert r.is_error and "Unexpected error from the mail backend: RuntimeError: odd server answer" in r.content[0].text
        assert "updated after this server process started" not in r.content[0].text
        monkeypatch.setattr(runtime, "_STARTED_WITH", -100.0)
        r = await c.call_tool("get_status", {})
        assert r.is_error and "Cannot reach Exchange." in r.content[0].text
        assert "updated after this server process started" in r.content[0].text and "Reconnect" in r.content[0].text
