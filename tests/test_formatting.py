"""Formatted email: Markdown in, HTML built by the server out. Safety comes first."""

import pytest
from mcp import Client

from owa_mail_mcp.formatting import misleading_links, to_html, to_plain
from owa_mail_mcp.models import Outgoing

from test_server import answer

pytestmark = pytest.mark.anyio


def test_markdown_becomes_simple_html():
    out = to_html("# Итоги\n\nПривет, **Анна**! Это *важно* и ~~неактуально~~, см. `budget_q4.xlsx`.\n"
                  "Вторая строка\n\n- первое\n- второе\n\n1. шаг\n2. шаг\n\n[Отчёт](https://corp.ru/report?a=1&b=2)", preview=False)
    assert out.startswith("<html><body><div style=")
    assert "<h3>Итоги</h3>" in out and "<b>Анна</b>" in out and "<i>важно</i>" in out and "<s>неактуально</s>" in out
    assert "<code>budget_q4.xlsx</code>" in out and "Привет" in out and "<br>" in out
    assert "<ul><li>первое</li><li>второе</li></ul>" in out and "<ol><li>шаг</li><li>шаг</li></ol>" in out
    assert '<a href="https://corp.ru/report?a=1&amp;b=2">Отчёт</a>' in out


@pytest.mark.parametrize("evil", [
    "<script>alert(1)</script>",
    "<img src=https://tracker.example/p.gif>",
    '<div style="display:none">hidden</div>',
    "<!-- hidden comment -->",
    "<a href='https://evil.example'>click</a>",
    '<p onclick="x()">x</p>',
])
def test_html_from_the_agent_is_shown_as_text(evil):
    out = to_html(evil, preview=True)
    assert "<" + evil[1:] not in out and "&lt;" in out


@pytest.mark.parametrize("text", [
    "[click](javascript:alert(1))",
    "[click](data:text/html;base64,PHNjcmlwdD4=)",
    "[click](vbscript:msgbox)",
    "![pixel](https://tracker.example/p.gif)",
])
def test_only_web_and_mail_links_are_made(text):
    out = to_html(text, preview=True)
    assert "<img" not in out and 'href="javascript' not in out and 'href="data' not in out and 'href="vbscript' not in out


def test_a_link_cannot_break_out_of_its_attribute():
    out = to_html('[x](https://a.ru/"onmouseover="alert(1))', preview=True)
    assert 'onmouseover="' not in out
    out = to_html("https://a.ru/x\"><script>alert(1)</script>", preview=True)
    assert "<script>" not in out


def test_preview_shows_where_links_lead():
    out = to_html("Войдите: [alfabank.ru](https://alfabank-login.example/) или https://corp.ru/a.", preview=True)
    assert '<span class="url">(https://alfabank-login.example/)</span>' in out
    assert '<a href="https://corp.ru/a">https://corp.ru/a</a>.' in out  # the full stop stays outside


def test_misleading_links_are_reported():
    assert misleading_links("[alfabank.ru](https://alfabank-login.example/)") == ["alfabank.ru → https://alfabank-login.example/"]
    assert misleading_links("[https://corp.ru/x](https://evil.example/x)") == ["https://corp.ru/x → https://evil.example/x"]
    assert misleading_links("[corp.ru](https://mail.corp.ru/owa) [Отчёт](https://evil.example)") == []


def test_snake_case_and_maths_are_left_alone():
    out = to_html("file_name_v2 and 2*3*4", preview=True)
    assert "<i>" not in out
    assert to_plain("**a** [b](https://c.ru)") == "a b (https://c.ru)"


def test_exchange_gets_html_only_for_markdown():
    from exchangelib import Body, HTMLBody

    from owa_mail_mcp.ews_backend import _body

    plain = _body(Outgoing(to=["a@b.ru"], body="**not bold**"))
    rich = _body(Outgoing(to=["a@b.ru"], body="**bold**", format="markdown"))
    assert type(plain) is Body and str(plain) == "**not bold**"
    assert isinstance(rich, HTMLBody) and "<b>bold</b>" in str(rich)


async def test_window_shows_the_formatted_message_and_sends_it(make_server, backend):
    mcp, tools = make_server()
    seen = []
    async with Client(mcp) as c:
        t = answer(tools, "approve", seen=seen)
        r = await c.call_tool("send_email", {"to": ["anna.smirnova@example.com"], "subject": "Итоги",
                                             "body": "**Решение:** сдвигаем релиз.\n\n[Детали](https://example.com/x)", "format": "markdown"})
        t.join()
    assert not r.is_error, r.content
    card = seen[0]
    assert "<b>Решение:</b>" in card["body_html"] and "(https://example.com/x)" in card["body_html"]
    assert card["message"]["format"] == "markdown" and backend.sent[-1].format == "markdown"


async def test_misleading_link_is_flagged_in_the_window(make_server):
    mcp, tools = make_server()
    seen = []
    async with Client(mcp) as c:
        t = answer(tools, "reject", seen=seen)
        await c.call_tool("send_email", {"to": ["anna.smirnova@example.com"], "subject": "Вход",
                                         "body": "[example.com](https://login.evil.test)", "format": "markdown"})
        t.join()
    assert "link_mismatch" in seen[0]["warning_codes"]


async def test_editing_keeps_the_forward_and_the_format(make_server, backend):
    mcp, tools = make_server()
    async with Client(mcp) as c:
        t = answer(tools, "approve", edits={"body": "**Посмотри**, пожалуйста."})
        r = await c.call_tool("forward_email", {"message_id": "demo-7", "to": ["maria.ivanova@example.com"],
                                                "comment": "Для сведения.", "format": "markdown"})
        t.join()
    assert not r.is_error, r.content
    sent = backend.sent[-1]
    assert sent.forward_id == "demo-7" and sent.format == "markdown" and sent.body == "**Посмотри**, пожалуйста."


async def test_trusted_recipients_still_see_misleading_links_in_the_window(make_server, backend):
    mcp, tools = make_server(trusted_recipients=("anna.smirnova@example.com",))
    seen = []
    async with Client(mcp) as c:
        ok = await c.call_tool("send_email", {"to": ["anna.smirnova@example.com"], "subject": "Итоги", "body": "**Готово**",
                                              "format": "markdown"})
        t = answer(tools, "reject", seen=seen)
        held = await c.call_tool("send_email", {"to": ["anna.smirnova@example.com"], "subject": "Вход",
                                                "body": "[example.com](https://login.evil.test)", "format": "markdown"})
        t.join()
    assert ok.structured_content["confirmed"] == "trusted_recipients"
    assert held.is_error and seen and "link_mismatch" in seen[0]["warning_codes"]
