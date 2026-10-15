from datetime import datetime, timedelta, timezone

import pytest

from owa_mail_mcp.ews_backend import ews_endpoint
from owa_mail_mcp.backend import BackendError
from owa_mail_mcp.models import Outgoing
from owa_mail_mcp.safety import PolicyError, check_outgoing, clean_addresses
from owa_mail_mcp.text import html_to_text, strip_quoted
from owa_mail_mcp.when import parse_when


def test_html_to_text_keeps_structure_and_drops_scripts():
    html = "<html><head><style>p{}</style></head><body><p>Hello&nbsp;<b>Ann</b></p><ul><li>one</li><li>two</li></ul><script>x()</script></body></html>"
    assert html_to_text(html) == "Hello Ann\n\n- one\n- two"


@pytest.mark.parametrize("body,expected", [
    ("Agreed.\n\nFrom: Anna Smirnova\nSent: Monday\nTo: Ivan\nSubject: Budget\n\nOld text", "Agreed."),
    ("Ок.\n\nОт: Анна\nОтправлено: понедельник\nТема: Бюджет\n\nстарое", "Ок."),
    ("Sure\n\nOn Mon, 12 Oct 2026 at 10:00, Anna <a@x.com> wrote:\n> old\n> older", "Sure"),
    ("Thanks\n-----Original Message-----\nFrom: x", "Thanks"),
    ("Reply\n> quoted\n> more", "Reply"),
])
def test_strip_quoted(body, expected):
    text, had = strip_quoted(body)
    assert text == expected and had


def test_strip_quoted_keeps_prose_mentioning_from():
    body = "Hi\nFrom: the report it follows that we are late.\nThanks"
    assert strip_quoted(body) == (body, False)


def test_parse_when():
    ref = datetime(2026, 10, 15, 13, 30, tzinfo=timezone.utc)
    assert parse_when("today", reference=ref) == datetime(2026, 10, 15, tzinfo=timezone.utc)
    assert parse_when("2026-10-01", end_of_day=True, reference=ref) == datetime(2026, 10, 2, tzinfo=timezone.utc)
    assert parse_when("7d", reference=ref) == ref - timedelta(days=7)
    assert parse_when("2026-10-01T09:00:00+03:00").utcoffset() == timedelta(hours=3)
    with pytest.raises(ValueError):
        parse_when("next tuesday")


def test_ews_endpoint():
    assert ews_endpoint("mail.corp.ru") == "https://mail.corp.ru/EWS/Exchange.asmx"
    assert ews_endpoint("https://mail.corp.ru/owa/") == "https://mail.corp.ru/EWS/Exchange.asmx"
    assert ews_endpoint("https://ex.corp.ru/EWS/Exchange.asmx") == "https://ex.corp.ru/EWS/Exchange.asmx"
    with pytest.raises(BackendError):
        ews_endpoint("http://mail.corp.ru")


def test_clean_addresses_splits_and_dedupes():
    assert clean_addresses(["a@x.com; B@x.com", "<a@x.com>"]) == ["a@x.com", "B@x.com"]


def _msg(**kw):
    base = dict(to=["anna@corp.ru"], subject="Hi", body="Text")
    base.update(kw)
    return Outgoing(**base)


def test_policy_flags_external_and_bcc():
    review = check_outgoing(_msg(to=["anna@corp.ru", "bob@gmail.com"], bcc=["c@corp.ru"]), my_email="me@corp.ru")
    assert review.external == ["bob@gmail.com"]
    assert any("Bcc" in w for w in review.warnings)


def test_policy_subdomain_is_internal():
    assert check_outgoing(_msg(to=["a@msk.corp.ru"]), my_email="me@corp.ru").external == []


@pytest.mark.parametrize("kw,needle", [
    ({"to": []}, "recipient"),
    ({"to": ["ivan@еxample.com"]}, "plain email"),  # Cyrillic е
    ({"to": ["not-an-address"]}, "plain email"),
    ({"subject": "  "}, "subject"),
    ({"body": "pay ‮ecnalab"}, "bidirectional"),
    ({"to": [f"u{i}@corp.ru" for i in range(51)]}, "Too many"),
])
def test_policy_refuses(kw, needle):
    with pytest.raises(PolicyError, match=needle):
        check_outgoing(_msg(**kw), my_email="me@corp.ru")


def test_policy_warns_on_hidden_chars_and_collapses_blank_lines():
    m = _msg(body="a​ b\n\n\n\n\nc")
    review = check_outgoing(m, my_email="me@corp.ru")
    assert m.body == "a​ b\n\nc"
    assert any("invisible" in w for w in review.warnings)
