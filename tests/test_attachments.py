import io
import zipfile

import pytest

from owa_mail_mcp.attachments import UnsupportedAttachment, extract_text


def _pdf(text: str) -> bytes:
    """A one-page PDF with a text layer, written by hand (no PDF writer needed)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for n, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % n + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref))
    return out.getvalue()


def test_pdf_text_layer():
    text, fmt = extract_text("notes.pdf", "application/pdf", _pdf("Meeting notes PBR"))
    assert fmt == "pdf" and "Meeting notes PBR" in text


def test_docx_paragraphs_tabs_and_breaks():
    doc = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        "<w:p><w:r><w:t>Строка 1</w:t><w:br/><w:t>Строка 2</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>A</w:t><w:tab/><w:t>B</w:t></w:r></w:p></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", doc)
    text, fmt = extract_text("t.docx", None, buf.getvalue())
    assert fmt == "docx" and text == "Строка 1\nСтрока 2\nA B"


def test_subtitles_keep_speech_only():
    vtt = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:04.000\n<v Анна>Начнём.\n\n2\n00:00:05.000 --> 00:00:07.500\n<v Олег>Ок.\n"
    text, fmt = extract_text("call.vtt", "text/vtt", vtt.encode())
    assert fmt == "vtt" and text == "Анна: Начнём.\nОлег: Ок."


def test_cp1251_text():
    text, _ = extract_text("old.txt", "text/plain", "Привет".encode("cp1251"))
    assert text == "Привет"


@pytest.mark.parametrize("name,ctype,needle", [
    ("photo.jpg", "image/jpeg", "not a text document"),
    ("old.doc", "application/msword", "old binary Office format"),
    ("broken.docx", None, "damaged"),
    ("scan.pdf", "application/pdf", "could not be read"),
])
def test_unsupported(name, ctype, needle):
    with pytest.raises(UnsupportedAttachment, match=needle):
        extract_text(name, ctype, b"\x00\x01 not really")
