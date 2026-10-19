"""EWSBackend against a tiny fake Exchange over HTTPS: exercises exchangelib for real, including
version negotiation, Basic auth, and certificate pinning."""

import base64
import re
import hashlib
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from owa_mail_mcp.backend import AuthenticationError, BackendError
from owa_mail_mcp.config import Settings
from owa_mail_mcp.ews_backend import CertificateProblem, EWSBackend

FIXTURES = Path(__file__).parent / "fixtures"
PASSWORD = "secret"

HEADER = (
    '<s:Header><h:ServerVersionInfo MajorVersion="15" MinorVersion="1" MajorBuildNumber="2507" MinorBuildNumber="6" '
    'Version="V2017_07_11" xmlns:h="http://schemas.microsoft.com/exchange/services/2006/types"/></s:Header>'
)
ENVELOPE = (
    '<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    'xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages" '
    'xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types">' + HEADER + "<s:Body>{body}</s:Body></s:Envelope>"
)
CONVERT_ID = (
    '<m:ConvertIdResponse><m:ResponseMessages><m:ConvertIdResponseMessage ResponseClass="Error">'
    "<m:MessageText>Invalid id.</m:MessageText><m:ResponseCode>ErrorInvalidIdMalformed</m:ResponseCode>"
    "<m:DescriptiveLinkKey>0</m:DescriptiveLinkKey></m:ConvertIdResponseMessage></m:ResponseMessages></m:ConvertIdResponse>"
)
RESOLVE_NAMES = (
    '<m:ResolveNamesResponse><m:ResponseMessages><m:ResolveNamesResponseMessage ResponseClass="Success">'
    '<m:ResponseCode>NoError</m:ResponseCode><m:ResolutionSet TotalItemsInView="1" IncludesLastItemInRange="true">'
    "<t:Resolution><t:Mailbox><t:Name>Иван Петров</t:Name><t:EmailAddress>ivan@corp.test</t:EmailAddress>"
    "<t:RoutingType>SMTP</t:RoutingType><t:MailboxType>Mailbox</t:MailboxType></t:Mailbox></t:Resolution>"
    "</m:ResolutionSet></m:ResolveNamesResponseMessage></m:ResponseMessages></m:ResolveNamesResponse>"
)


FOLDERS = {
    "root": ("root-id", "Top of Information Store", "IPF.Note", 0, 0),
    "msgfolderroot": ("msgroot-id", "Top of Information Store", "IPF.Note", 0, 0),
    "inbox": ("inbox-id", "Входящие", "IPF.Note", 3, 2),
    "sentitems": ("sent-id", "Отправленные", "IPF.Note", 1, 0),
    "deleteditems": ("trash-id", "Удалённые", "IPF.Note", 0, 0),
    "junkemail": ("junk-id", "Нежелательная почта", "IPF.Note", 0, 0),
    "drafts": ("drafts-id", "Черновики", "IPF.Note", 0, 0),
    "outbox": ("outbox-id", "Исходящие", "IPF.Note", 0, 0),
}
SENT = [
    ("msg-s1", "RE: Бюджет Q4", "Иван Петров", "ivan@corp.test", "2026-10-14T10:00:00Z", "true", "Анна, по подрядчикам согласен."),
]
MESSAGES = [
    ("msg-1", "RE: Бюджет Q4", "Анна Смирнова", "anna@corp.test", "2026-10-15T08:00:00Z", "false", "Ок, ставлю встречу на четверг."),
    ("msg-2", "Отпуск в ноябре", "Мария Иванова", "maria@corp.test", "2026-10-14T16:30:00Z", "false", "Подтвердите, пожалуйста, отпуск."),
    ("msg-3", "Инцидент: разбор", "Олег Кузнецов", "oleg@corp.test", "2026-10-13T09:15:00Z", "true", "Постмортем в приложении."),
]


def _folder_xml(name):
    fid, display, cls, total, unread = FOLDERS[name]
    return (
        f'<t:Folder><t:FolderId Id="{fid}" ChangeKey="ck"/><t:ParentFolderId Id="root-id" ChangeKey="ck"/>'
        f"<t:FolderClass>{cls}</t:FolderClass><t:DisplayName>{display}</t:DisplayName>"
        f"<t:TotalCount>{total}</t:TotalCount><t:ChildFolderCount>0</t:ChildFolderCount><t:UnreadCount>{unread}</t:UnreadCount></t:Folder>"
    )


def get_folder_response(body):
    names = re.findall(r'DistinguishedFolderId Id="(\w+)"', body)
    by_id = {v[0]: k for k, v in FOLDERS.items()}
    names += [by_id[i] for i in re.findall(r'FolderId Id="([^"]+)"', body) if i in by_id]
    messages = "".join(
        '<m:GetFolderResponseMessage ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode>'
        f"<m:Folders>{_folder_xml(n)}</m:Folders></m:GetFolderResponseMessage>"
        if n in FOLDERS else
        '<m:GetFolderResponseMessage ResponseClass="Error"><m:MessageText>Not found</m:MessageText>'
        "<m:ResponseCode>ErrorFolderNotFound</m:ResponseCode><m:DescriptiveLinkKey>0</m:DescriptiveLinkKey></m:GetFolderResponseMessage>"
        for n in names
    )
    return f"<m:GetFolderResponse><m:ResponseMessages>{messages}</m:ResponseMessages></m:GetFolderResponse>"


def _message_xml(m, full):
    mid, subject, name, email, received, is_read, body = m
    extra = f'<t:Body BodyType="Text">{body}</t:Body>' if full else ""
    if full and mid == "msg-1":
        extra += ('<t:Attachments><t:FileAttachment><t:AttachmentId Id="att-1"/><t:Name>transcript.docx</t:Name>'
                  "<t:Size>2048</t:Size></t:FileAttachment></t:Attachments>")
    conversation = '<t:ConversationId Id="conv-budget"/>' if "Бюджет" in subject else f'<t:ConversationId Id="conv-{mid}"/>'
    return (
        f'<t:Message><t:ItemId Id="{mid}" ChangeKey="ck"/><t:Subject>{subject}</t:Subject>{extra}'
        f"<t:DateTimeReceived>{received}</t:DateTimeReceived><t:HasAttachments>false</t:HasAttachments>"
        f"<t:Importance>Normal</t:Importance>"
        f"<t:Sender><t:Mailbox><t:Name>{name}</t:Name><t:EmailAddress>{email}</t:EmailAddress></t:Mailbox></t:Sender>"
        f"<t:IsRead>{is_read}</t:IsRead>{conversation}</t:Message>"
    )


def transcript_docx() -> bytes:
    import io
    import zipfile

    doc = (
        '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>Протокол встречи PBR</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Анна:</w:t></w:r><w:r><w:tab/><w:t>бюджет согласован.</w:t></w:r></w:p></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def get_attachment_response():
    content = base64.b64encode(transcript_docx()).decode()
    return (
        '<m:GetAttachmentResponse><m:ResponseMessages><m:GetAttachmentResponseMessage ResponseClass="Success">'
        "<m:ResponseCode>NoError</m:ResponseCode><m:Attachments><t:FileAttachment>"
        '<t:AttachmentId Id="att-1"/><t:Name>transcript.docx</t:Name>'
        "<t:ContentType>application/vnd.openxmlformats-officedocument.wordprocessingml.document</t:ContentType>"
        f"<t:Content>{content}</t:Content></t:FileAttachment></m:Attachments>"
        "</m:GetAttachmentResponseMessage></m:ResponseMessages></m:GetAttachmentResponse>"
    )


def find_item_response(body=""):
    parents = re.search(r"<m:ParentFolderIds>(.*?)</m:ParentFolderIds>", body, re.S)
    ids = re.findall(r'(?:Distinguished)?FolderId Id="([^"]+)"', parents.group(1)) if parents else []
    if len(ids) > 1:  # what the bank's Exchange says to a multi-folder search
        return (
            '<m:FindItemResponse><m:ResponseMessages><m:FindItemResponseMessage ResponseClass="Error">'
            "<m:MessageText>Shared folder search cannot be performed on multiple folders.</m:MessageText>"
            "<m:ResponseCode>ErrorInvalidOperation</m:ResponseCode><m:DescriptiveLinkKey>0</m:DescriptiveLinkKey>"
            "</m:FindItemResponseMessage></m:ResponseMessages></m:FindItemResponse>"
        )
    pool = SENT if ids and ids[0] in ("sent-id", "sentitems") else MESSAGES if ids and ids[0] in ("inbox-id", "inbox") else []
    if "item:ConversationId" in body:
        conv = re.search(r'Constant Value="([^"]+)"', body)
        pool = [m for m in pool if conv and ("Бюджет" in m[1]) == (conv.group(1) == "conv-budget")]
    items = "".join(_message_xml(m, False) for m in pool)
    return (
        '<m:FindItemResponse><m:ResponseMessages><m:FindItemResponseMessage ResponseClass="Success">'
        f'<m:ResponseCode>NoError</m:ResponseCode><m:RootFolder TotalItemsInView="{len(pool)}" IncludesLastItemInRange="true">'
        f"<t:Items>{items}</t:Items></m:RootFolder></m:FindItemResponseMessage></m:ResponseMessages></m:FindItemResponse>"
    )


def get_item_response(ids):
    by_id = {m[0]: m for m in MESSAGES + SENT}
    messages = "".join(
        '<m:GetItemResponseMessage ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode>'
        f"<m:Items>{_message_xml(by_id[i], True)}</m:Items></m:GetItemResponseMessage>"
        for i in ids if i in by_id
    )
    return f"<m:GetItemResponse><m:ResponseMessages>{messages}</m:ResponseMessages></m:GetItemResponse>"


class FakeExchange(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        expected = "Basic " + base64.b64encode(f"CORP\\ivan:{PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") != expected:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="ews"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if "ConvertId" in body:
            answer = CONVERT_ID
        elif "ResolveNames" in body:
            answer = RESOLVE_NAMES
        elif "GetFolder" in body:
            answer = get_folder_response(body)
        elif "FindFolder" in body:
            answer = (
                '<m:FindFolderResponse><m:ResponseMessages><m:FindFolderResponseMessage ResponseClass="Success">'
                '<m:ResponseCode>NoError</m:ResponseCode><m:RootFolder TotalItemsInView="0" IncludesLastItemInRange="true">'
                "<t:Folders/></m:RootFolder></m:FindFolderResponseMessage></m:ResponseMessages></m:FindFolderResponse>"
            )
        elif "FindItem" in body:
            answer = find_item_response(body)
        elif "GetAttachment" in body:
            answer = get_attachment_response()
        elif "GetItem" in body:
            answer = get_item_response(re.findall(r'ItemId Id="([^"]+)"', body))
        else:  # any other operation: a SOAP error with version headers, as Exchange does
            answer = (
                '<m:GetFolderResponse><m:ResponseMessages><m:GetFolderResponseMessage ResponseClass="Success">'
                "<m:ResponseCode>NoError</m:ResponseCode></m:GetFolderResponseMessage></m:ResponseMessages></m:GetFolderResponse>"
            )
        data = ENVELOPE.format(body=answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def exchange():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeExchange)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(FIXTURES / "ews-cert.pem", FIXTURES / "ews-key.pem")
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    der = ssl.PEM_cert_to_DER_cert((FIXTURES / "ews-cert.pem").read_text())
    yield server.server_address[1], hashlib.sha256(der).hexdigest()
    server.shutdown()


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


def _settings(port, fingerprint, password=PASSWORD, pin=True):
    return Settings(
        server=f"https://localhost:{port}/EWS/Exchange.asmx", email="ivan@corp.test", username="CORP\\ivan",
        password=password, auth_type="basic",
        trusted_certificates={f"localhost:{port}": fingerprint} if pin else {},
    )


def test_whoami_negotiates_version_first(exchange):
    port, fingerprint = exchange
    me = EWSBackend(_settings(port, fingerprint)).whoami()
    assert me.email == "ivan@corp.test" and me.name == "Иван Петров"


def test_find_people_on_a_fresh_backend(exchange):
    port, fingerprint = exchange
    people = EWSBackend(_settings(port, fingerprint)).find_people("Петров", 5)
    assert people[0].email == "ivan@corp.test"


def test_wrong_password_is_an_authentication_error(exchange):
    port, fingerprint = exchange
    with pytest.raises(AuthenticationError):
        EWSBackend(_settings(port, fingerprint, password="wrong")).whoami()


def test_unpinned_self_signed_certificate_is_refused(exchange):
    port, fingerprint = exchange
    with pytest.raises(CertificateProblem):
        EWSBackend(_settings(port, fingerprint, pin=False)).whoami()
    with pytest.raises(CertificateProblem):
        EWSBackend(_settings(port, "00" * 32)).whoami()


class GatedExchange(FakeExchange):
    """Like the bank's front end: EWS requests on a connection that has not signed in to OWA are
    dropped without an answer; after GET /owa/ with valid credentials, the connection is let through."""

    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.signed_in = False

    def do_GET(self):  # noqa: N802
        expected = "Basic " + base64.b64encode(f"CORP\\ivan:{PASSWORD}".encode()).decode()
        if self.path.startswith("/owa/") and self.headers.get("Authorization") == expected:
            self.signed_in = True
            body = b"<html><title>Outlook</title></html>"
            self.send_response(200)
        else:
            body = b""
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="owa"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        if not self.signed_in:
            self.close_connection = True  # no answer at all
            return
        super().do_POST()


@pytest.fixture
def gated_exchange():
    server = ThreadingHTTPServer(("127.0.0.1", 0), GatedExchange)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(FIXTURES / "ews-cert.pem", FIXTURES / "ews-key.pem")
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    der = ssl.PEM_cert_to_DER_cert((FIXTURES / "ews-cert.pem").read_text())
    yield server.server_address[1], hashlib.sha256(der).hexdigest()
    server.shutdown()


def test_ews_behind_a_front_end_that_wants_owa_sign_in_first(gated_exchange):
    port, fingerprint = gated_exchange
    backend = EWSBackend(_settings(port, fingerprint))
    assert backend.whoami().email == "ivan@corp.test"
    assert backend.find_people("Петров", 5)[0].name == "Иван Петров"


class NotFoundUntilSignedIn(GatedExchange):
    """A front end that answers 404 (instead of dropping) EWS on a connection not signed in to OWA."""

    def do_POST(self):  # noqa: N802
        if not self.signed_in:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        FakeExchange.do_POST(self)


class AlwaysNotFound(GatedExchange):
    """Outside the corporate network: web mail is published, EWS is not."""

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(404)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", "9")
        self.end_headers()
        self.wfile.write(b"not found")


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(FIXTURES / "ews-cert.pem", FIXTURES / "ews-key.pem")
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    der = ssl.PEM_cert_to_DER_cert((FIXTURES / "ews-cert.pem").read_text())
    return server, server.server_address[1], hashlib.sha256(der).hexdigest()


def test_a_404_before_owa_sign_in_is_retried_after_it():
    server, port, fingerprint = _serve(NotFoundUntilSignedIn)
    try:
        assert EWSBackend(_settings(port, fingerprint)).whoami().email == "ivan@corp.test"
    finally:
        server.shutdown()


def test_a_404_says_the_server_answered_instead_of_blaming_the_network():
    server, port, fingerprint = _serve(AlwaysNotFound)
    try:
        with pytest.raises(BackendError) as err:
            EWSBackend(_settings(port, fingerprint)).whoami()
    finally:
        server.shutdown()
    text = str(err.value)
    assert "HTTP 404" in text and "Cannot reach" not in text and "not found" not in text and "diagnose" in text


def test_diagnosis_finds_that_owa_sign_in_opens_ews(gated_exchange):
    from owa_mail_mcp.diagnose import diagnose

    port, fingerprint = gated_exchange
    settings = _settings(port, fingerprint)
    result = diagnose(settings, f"https://localhost:{port}/EWS/Exchange.asmx")
    assert "только на соединении, которое сначала вошло в OWA" in result.summary, result.text()
    assert PASSWORD not in result.text()


def test_connection_test_end_to_end(exchange):
    """What the setup window's "Test connection" does: who am I, unread count, latest messages."""
    from owa_mail_mcp.backend import SearchQuery

    port, fingerprint = exchange
    backend = EWSBackend(_settings(port, fingerprint))
    assert backend.whoami().name == "Иван Петров"
    assert backend.unread_count() == 2
    latest = backend.search(SearchQuery(folder="inbox", limit=3)).messages
    assert [m.subject for m in latest] == ["RE: Бюджет Q4", "Отпуск в ноябре", "Инцидент: разбор"]
    assert latest[0].sender.email == "anna@corp.test" and not latest[0].is_read
    assert latest[0].preview.startswith("Ок, ставлю встречу")


def test_setup_window_test_connection_against_exchange(exchange, tmp_path, monkeypatch):
    """The setup window's handler returns plain JSON for a real exchangelib backend."""
    import json

    from owa_mail_mcp import config, setup as setup_mod
    from owa_mail_mcp.setup import SetupApp
    from owa_mail_mcp.tls import CertificateInfo

    port, fingerprint = exchange
    monkeypatch.setenv("OWA_MAIL_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config, "get_password", lambda login: None)
    monkeypatch.setattr(setup_mod, "get_password", lambda login: None)
    config.save_settings(Settings(server="x", email="ivan@corp.test", trusted_certificates={f"localhost:{port}": fingerprint}))
    app = SetupApp(prober=lambda h, p, legacy_ok=False: CertificateInfo(h, p, fingerprint, False))
    status, result = app.test_connection({
        "server": f"https://localhost:{port}/EWS/Exchange.asmx", "email": "ivan@corp.test",
        "username": "CORP\\ivan", "password": PASSWORD, "auth_type": "basic",
    })
    json.dumps(result)  # what failed before: a folder object instead of a number
    assert result["ok"], result
    assert result["unread"] == 2 and len(result["latest"]) == 3


def test_reading_a_message_with_an_attachment(exchange):
    port, fingerprint = exchange
    msg = EWSBackend(_settings(port, fingerprint)).get_message("msg-1")
    assert msg.subject == "RE: Бюджет Q4" and msg.body.startswith("Ок, ставлю встречу")
    assert msg.attachments == ["transcript.docx"]


def test_thread_and_all_folder_search_go_folder_by_folder(exchange):
    from owa_mail_mcp.backend import SearchQuery

    port, fingerprint = exchange
    backend = EWSBackend(_settings(port, fingerprint))
    thread = backend.get_thread("msg-1", 30)
    assert [m.id for m in thread] == ["msg-s1", "msg-1"]  # Sent and Inbox, oldest first
    everywhere = backend.search(SearchQuery(folder="all", limit=10)).messages
    assert {m.id for m in everywhere} == {"msg-1", "msg-2", "msg-3", "msg-s1"}
    assert everywhere[0].id == "msg-1"  # newest first across folders
    both = backend.search(SearchQuery(folder="inbox_and_sent", limit=2)).messages
    assert [m.id for m in both] == ["msg-1", "msg-2"]
    sent = backend.scan_headers("sent", None, None, 50)
    assert [m.id for m in sent] == ["msg-s1"]


@pytest.mark.anyio
async def test_mcp_tools_read_messages_and_threads(exchange):
    from mcp import Client

    from owa_mail_mcp.server import build_server

    port, fingerprint = exchange
    mcp, _ = build_server(_settings(port, fingerprint), EWSBackend(_settings(port, fingerprint)))
    async with Client(mcp) as c:
        r = await c.call_tool("get_message", {"message_id": "msg-1"})
        assert not r.is_error, r.content
        assert r.structured_content["attachments"] == ["transcript.docx"]
        r = await c.call_tool("get_thread", {"message_id": "msg-1"})
        assert not r.is_error, r.content
        assert r.structured_content["message_count"] == 2
        r = await c.call_tool("search_messages", {"text": "бюджет", "folder": "all"})
        assert not r.is_error, r.content
        r = await c.call_tool("get_attachment", {"message_id": "msg-1", "attachment": "transcript.docx"})
        assert not r.is_error, r.content
        assert r.structured_content["format"] == "docx"
        assert r.structured_content["text"] == "Протокол встречи PBR\nАнна: бюджет согласован."
        r = await c.call_tool("get_attachment", {"message_id": "msg-1", "attachment": "nope.pdf"})
        assert r.is_error and "transcript.docx" in r.content[0].text


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_requested_fields_exist_in_exchangelib():
    """A misspelt field fails only against a real server, before any request is made."""
    from exchangelib import Message

    from owa_mail_mcp import ews_backend

    names = {f.name for f in Message.FIELDS} | {"id", "changekey"}
    for group in (ews_backend._HEADER_FIELDS, ews_backend._SUMMARY_FIELDS, ews_backend._FULL_FIELDS):
        assert set(group) <= names, set(group) - names
