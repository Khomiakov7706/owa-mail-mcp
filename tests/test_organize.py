import json

import pytest
from mcp import Client

from owa_mail_mcp.config import Settings
from owa_mail_mcp.server import build_server

from test_server import answer

pytestmark = pytest.mark.anyio


async def test_organizing_is_refused_unless_enabled(backend):
    mcp, _ = build_server(Settings(backend="demo", allow_organize=False), backend)
    async with Client(mcp) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        results = [
            await c.call_tool("create_draft", {"body": "x"}),
            await c.call_tool("update_messages", {"message_ids": ["demo-1"], "read": True}),
            await c.call_tool("move_messages", {"message_ids": ["demo-1"], "folder": "archive"}),
            await c.call_tool("delete_messages", {"message_ids": ["demo-1"]}),
        ]
    assert {"create_draft", "update_messages", "move_messages", "delete_messages", "list_folders", "forward_email"} <= names
    assert all(r.is_error and "Organizing mail is turned off" in r.content[0].text for r in results)
    assert backend.drafts == [] and backend.get_message("demo-1").folder == "Inbox"


async def test_drafts_new_and_reply_need_no_window(make_server, backend):
    mcp, tools = make_server(allow_organize=True)
    async with Client(mcp) as c:
        new = await c.call_tool("create_draft", {"to": ["anna.smirnova@example.com"], "subject": "Q4", "body": "Черновик"})
        reply = await c.call_tool("create_draft", {"body": "Оценку пришлю завтра.", "reply_to_message_id": "demo-4", "reply_all": True})
        bad = await c.call_tool("create_draft", {"to": ["not an address"], "body": "x"})
    assert new.structured_content["saved"] is True and new.structured_content["message_id"]
    assert reply.structured_content["to"] == ["anna.smirnova@example.com"] and reply.structured_content["subject"] == "RE: Бюджет Q4: черновик"
    assert backend.drafts[1].reply_to_id == "demo-4" and backend.sent == []
    assert bad.is_error and "Not a plain email address" in bad.content[0].text
    assert tools.broker.snapshot() == []  # no confirmation was asked


async def test_mark_flag_categorize_move_delete(make_server, backend):
    mcp, _ = make_server(allow_organize=True)
    async with Client(mcp) as c:
        r = await c.call_tool("update_messages", {"message_ids": ["demo-3", "demo-5"], "read": True, "flag": "flagged", "add_categories": ["Бюджет"]})
        assert r.structured_content == {"updated": 2, "requested": 2}
        checked = (await c.call_tool("search_messages", {"folder": "inbox", "sender": "Мария"})).structured_content["messages"][0]
        assert checked["is_read"] is True and checked["flagged"] is True
        assert backend.get_message("demo-3").categories == ["Бюджет"]
        nothing = await c.call_tool("update_messages", {"message_ids": ["demo-3"]})
        assert nothing.is_error

        moved = (await c.call_tool("move_messages", {"message_ids": ["demo-6"], "folder": "archive"})).structured_content
        assert moved["folder"] == "Archive" and moved["new_message_ids"][0] != "demo-6"
        sub = (await c.call_tool("move_messages", {"message_ids": ["demo-7"], "folder": "Бюджет"})).structured_content
        assert sub["folder"] == "Проекты/Бюджет"
        gone = (await c.call_tool("delete_messages", {"message_ids": ["demo-5"]})).structured_content
        assert gone["folder"] == "Deleted Items"
        folders = {f["path"]: f for f in (await c.call_tool("list_folders", {})).structured_content["folders"]}
        assert folders["Archive"]["total"] == 1 and folders["Deleted Items"]["total"] == 1
        missing = await c.call_tool("move_messages", {"message_ids": ["demo-1"], "folder": "Нет такой"})
        assert missing.is_error and "list_folders" in missing.content[0].text
        inbox = (await c.call_tool("search_messages", {"folder": "all", "sender": "Мария"})).structured_content["messages"]
        assert inbox == []  # Deleted Items is not searched with folder=all


async def test_forward_goes_through_the_window_with_attachments(make_server, backend):
    mcp, tools = make_server()
    seen = []
    async with Client(mcp) as c:
        t = answer(tools, "approve", seen=seen)
        r = await c.call_tool("forward_email", {"message_id": "demo-7", "to": ["maria.ivanova@example.com"], "comment": "Для сведения."})
        t.join()
    assert not r.is_error, r.content
    assert r.structured_content["subject"] == "FW: Инцидент в проде: разбор" and r.structured_content["confirmed"] == "user"
    card = seen[0]
    assert card["reply_context"]["forward"] is True and card["reply_context"]["attachments"] == ["postmortem.txt"]
    assert backend.sent[0].forward_id == "demo-7" and backend.sent[0].body == "Для сведения."


async def test_trusted_recipients_skip_the_window_and_only_them(make_server, backend):
    mcp, tools = make_server(trusted_recipients=("anna.smirnova@example.com", "oleg.kuznetsov@example.com"), trusted_per_hour=2)
    async with Client(mcp) as c:
        first = await c.call_tool("send_email", {"to": ["Anna.Smirnova@example.com"], "cc": ["oleg.kuznetsov@example.com"], "subject": "Q4", "body": "Ок"})
        assert first.structured_content["confirmed"] == "trusted_recipients"
        assert tools.broker.snapshot()[0]["auto"] is True and tools.broker.snapshot()[0]["status"] == "sent"

        # Someone not on the list: back to the window.
        t = answer(tools, "reject")
        mixed = await c.call_tool("send_email", {"to": ["anna.smirnova@example.com", "john.doe@partner.org"], "subject": "Q4", "body": "Ок"})
        t.join()
        assert mixed.is_error and "declined" in mixed.content[0].text

        # Invisible characters need a human look even for trusted people.
        t = answer(tools, "reject")
        hidden = await c.call_tool("send_email", {"to": ["anna.smirnova@example.com"], "subject": "Q4", "body": "О​к"})
        t.join()
        assert hidden.is_error

        second = await c.call_tool("send_email", {"to": ["oleg.kuznetsov@example.com"], "subject": "Q4", "body": "Ещё"})
        assert second.structured_content["confirmed"] == "trusted_recipients"
        # The hourly budget is spent: the third one waits for the user again.
        t = answer(tools, "approve")
        third = await c.call_tool("send_email", {"to": ["oleg.kuznetsov@example.com"], "subject": "Q4", "body": "И ещё"})
        t.join()
        assert third.structured_content["confirmed"] == "user"
    assert len(backend.sent) == 3


async def test_trusted_list_does_nothing_while_sending_is_off(backend):
    mcp, _ = build_server(Settings(backend="demo", allow_send=False, trusted_recipients=("anna.smirnova@example.com",)), backend)
    async with Client(mcp) as c:
        r = await c.call_tool("send_email", {"to": ["anna.smirnova@example.com"], "subject": "Q4", "body": "Ок"})
    assert r.is_error and "turned off" in r.content[0].text and backend.sent == []


def test_setup_saves_the_trusted_list_and_refuses_bad_addresses(tmp_path, monkeypatch):
    from owa_mail_mcp import config
    from owa_mail_mcp.setup import SetupApp

    monkeypatch.setenv("OWA_MAIL_CONFIG_DIR", str(tmp_path))
    app = SetupApp()
    _, r = app.save_access({"allow_send": True, "allow_organize": True, "trusted_recipients": "Anna@corp.ru\n<oleg@corp.ru>, anna@corp.ru"})
    assert r == {"ok": True}
    saved = json.loads(config.config_path().read_text())
    assert saved["trusted_recipients"] == ["anna@corp.ru", "oleg@corp.ru"] and saved["allow_organize"] is True
    assert app.state()["access"]["trusted_recipients"] == "anna@corp.ru\noleg@corp.ru"
    _, r = app.save_access({"trusted_recipients": "anna@corp.ru\nanna"})
    assert r["ok"] is False and "anna" in r["error"]
    assert json.loads(config.config_path().read_text())["trusted_recipients"] == ["anna@corp.ru", "oleg@corp.ru"]


def test_exchange_flag_property_and_folder_names(monkeypatch):
    from types import SimpleNamespace

    from exchangelib import Message

    from owa_mail_mcp.ews_backend import EWSBackend

    assert "flag_status" in {f.name for f in Message.FIELDS}
    backend = EWSBackend(Settings(server="mail.corp.ru", email="me@corp.ru", password="x"))
    folders = {name: SimpleNamespace(name=name.rsplit("/", 1)[-1]) for name in ("Inbox", "Архив", "Проекты", "Проекты/Q4", "Inbox/Q4")}
    monkeypatch.setattr(backend, "_mail_folders", lambda: list(folders.items()))
    trash = SimpleNamespace(name="Deleted Items")
    backend._account = SimpleNamespace(trash=trash, inbox=folders["Inbox"])
    assert backend._resolve_folder("archive") is folders["Архив"]
    assert backend._resolve_folder("Удалённые") is trash
    assert backend._resolve_folder("проекты/q4") is folders["Проекты/Q4"]
    with pytest.raises(Exception, match="several folders"):
        backend._resolve_folder("Q4")
