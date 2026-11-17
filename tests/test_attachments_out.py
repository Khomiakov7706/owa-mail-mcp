"""Attaching files to outgoing email: only real documents and images, from ordinary folders, within limits."""

import io
import zipfile
from types import SimpleNamespace

import pytest
from mcp import Client

from owa_mail_mcp import outgoing_files
from owa_mail_mcp.models import Outgoing
from owa_mail_mcp.outgoing_files import AttachmentError, load_file, load_files

from test_server import answer

pytestmark = pytest.mark.anyio
PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _ooxml(part: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr(f"{part}workbook.xml", "<x/>")
    return buf.getvalue()


@pytest.fixture
def files(tmp_path):
    def make(name: str, data: bytes):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)
    return make


def test_common_documents_are_read(files):
    got = load_files([files("report.pdf", PDF), files("chart.png", PNG), files("budget.xlsx", _ooxml("xl/")),
                      files("data.csv", "дата;сумма\n01.11;100\n".encode("utf-8"))])
    assert [(f.name, f.content_type) for f in got] == [
        ("report.pdf", "application/pdf"), ("chart.png", "image/png"),
        ("budget.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"), ("data.csv", "text/csv")]
    assert got[0].data == PDF


@pytest.mark.parametrize("name,data,reason", [
    ("tool.exe", b"MZ\x90\x00", "cannot be attached"),
    ("archive.zip", b"PK\x03\x04", "cannot be attached"),
    ("fake.pdf", b"just some bytes", "not a real PDF"),
    ("fake.xlsx", _ooxml("word/"), "not a real XLSX"),
    ("key.txt", b"-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n", "not a real TXT"),
    ("photo.jpg", PNG, "not a real JPG"),
    ("empty.pdf", b"", "empty"),
    (".ssh/id.pdf", PDF, "hidden folders"),
    ("re‮port.pdf", PDF, "invisible"),
])
def test_what_is_refused(files, name, data, reason):
    with pytest.raises(AttachmentError, match=reason):
        load_file(files(name, data))


def test_paths_and_limits(files, tmp_path, monkeypatch):
    with pytest.raises(AttachmentError, match="full path"):
        load_file("report.pdf")
    with pytest.raises(AttachmentError, match="no such file"):
        load_file(str(tmp_path / "missing.pdf"))
    key = files("secret.txt", b"password")
    link = tmp_path / "innocent.pdf"
    link.symlink_to(key)
    with pytest.raises(AttachmentError, match="another type"):
        load_file(str(link))
    monkeypatch.setattr(outgoing_files, "_roots", lambda: [tmp_path / "allowed"])
    with pytest.raises(AttachmentError, match="only files from your home"):
        load_file(files("elsewhere.pdf", PDF))
    monkeypatch.undo()
    monkeypatch.setattr(outgoing_files, "MAX_FILE_BYTES", 10)
    with pytest.raises(AttachmentError, match="larger than"):
        load_file(files("big.pdf", PDF))
    monkeypatch.undo()
    with pytest.raises(AttachmentError, match="same name"):
        load_files([files("a/x.pdf", PDF), files("b/X.pdf", PDF)])


async def test_window_shows_files_and_the_user_can_take_one_off(make_server, backend, files):
    mcp, tools = make_server()
    seen = []
    paths = [files("report.pdf", PDF), files("chart.png", PNG)]
    async with Client(mcp) as c:
        t = answer(tools, "approve", seen=seen, edits={"attachments": ["report.pdf"]})
        r = await c.call_tool("send_email", {"to": ["john.doe@partner.org"], "subject": "Отчёт", "body": "Во вложении.", "attachments": paths})
        t.join()
        bad = await c.call_tool("send_email", {"to": ["anna.smirnova@example.com"], "subject": "x", "body": "y",
                                               "attachments": [files("run.sh", b"#!/bin/sh")]})
    assert not r.is_error, r.content
    card = seen[0]["message"]["attachments"]
    assert [(a["name"], a["size"]) for a in card] == [("report.pdf", len(PDF)), ("chart.png", len(PNG))] and card[0]["path"]
    assert "files_external" in seen[0]["warning_codes"]
    assert r.structured_content["attachments"] == ["report.pdf"] and r.structured_content["edited_by_user"] is True
    assert [a.name for a in backend.sent[-1].attachments] == ["report.pdf"]
    assert bad.is_error and "cannot be attached" in bad.content[0].text and "Nothing was sent" in bad.content[0].text


async def test_draft_with_a_file(make_server, backend, files):
    mcp, _ = make_server(allow_organize=True)
    async with Client(mcp) as c:
        r = await c.call_tool("create_draft", {"to": ["anna.smirnova@example.com"], "subject": "Черновик", "body": "Текст",
                                               "attachments": [files("plan.pdf", PDF)]})
    assert not r.is_error, r.content
    assert r.structured_content["attachments"] == ["plan.pdf"]


def test_exchange_attaches_files_to_new_messages_and_replies(files):
    from owa_mail_mcp.ews_backend import EWSBackend, _files
    from owa_mail_mcp.config import Settings

    message = Outgoing(to=["a@corp.ru"], subject="RE: x", body="b", reply_to_id="AAMk=", attachments=load_files([files("r.pdf", PDF)]))
    made = _files(message)
    assert made[0].name == "r.pdf" and made[0].content == PDF and made[0].content_type == "application/pdf"

    calls = []
    draft = SimpleNamespace(attach=lambda f: calls.append(("attach", f.name)), send=lambda **kw: calls.append(("send", kw)))
    backend = EWSBackend(Settings(server="mail.corp.ru", email="me@corp.ru", password="x"))
    backend._account = SimpleNamespace(sent="SENT", drafts="DRAFTS")
    backend._reply_draft = lambda *a: draft
    backend.send(message)
    assert calls == [("attach", "r.pdf"), ("send", {"save_copy": True, "copy_to_folder": "SENT"})]
