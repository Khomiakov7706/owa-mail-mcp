"""Text out of attachments: meeting transcripts, documents, notes.

Plain text formats are decoded; Word (.docx) is read from its XML with the standard library;
PDF through pypdf. Images, archives and old binary Office formats are not read.
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from xml.etree import ElementTree

from .text import html_to_text, normalize_whitespace

MAX_BYTES = 15 * 1024 * 1024

_TEXT_EXTENSIONS = {".txt", ".text", ".md", ".csv", ".tsv", ".log", ".json", ".xml", ".vtt", ".srt", ".ics", ".yaml", ".yml"}
_HTML_EXTENSIONS = {".html", ".htm"}
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class UnsupportedAttachment(ValueError):
    pass


def extract_text(name: str, content_type: str | None, data: bytes) -> tuple[str, str]:
    """Returns (text, format)."""
    ext = os.path.splitext(name or "")[1].lower()
    ctype = (content_type or "").lower()
    if ext == ".docx" or "wordprocessingml" in ctype:
        return _docx(data), "docx"
    if ext == ".pdf" or ctype == "application/pdf":
        return _pdf(data), "pdf"
    if ext in _HTML_EXTENSIONS or ctype == "text/html":
        return html_to_text(_decode(data)), "html"
    if ext in _TEXT_EXTENSIONS or ctype.startswith("text/") or ctype in {"application/json", "application/xml"}:
        text = _decode(data)
        if ext == ".vtt" or ext == ".srt":
            text = _subtitles(text)
        return normalize_whitespace(text), ext.lstrip(".") or "text"
    if ext in {".doc", ".xls", ".ppt", ".rtf"}:
        raise UnsupportedAttachment(f"{name}: old binary Office format, not readable here. Ask the sender for .docx or PDF.")
    raise UnsupportedAttachment(f"{name}: {content_type or ext or 'unknown type'} is not a text document.")


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _docx(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise UnsupportedAttachment("The .docx file is damaged or not a Word document.") from exc
    root = ElementTree.fromstring(xml)
    paragraphs = []
    for p in root.iter(f"{_W}p"):
        parts = []
        for node in p.iter():
            if node.tag == f"{_W}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{_W}tab":
                parts.append("\t")
            elif node.tag in (f"{_W}br", f"{_W}cr"):
                parts.append("\n")
        paragraphs.append("".join(parts))
    return normalize_whitespace("\n".join(paragraphs))


def _pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise UnsupportedAttachment("Reading PDF needs the pypdf package.") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise UnsupportedAttachment("The PDF is password-protected.")
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except UnsupportedAttachment:
        raise
    except Exception as exc:
        raise UnsupportedAttachment(f"The PDF could not be read: {type(exc).__name__}.") from exc
    text = normalize_whitespace(text)
    if not text:
        raise UnsupportedAttachment("The PDF has no text layer (probably a scan).")
    return text


_TIMING = re.compile(r"^\s*(\d+\s*)?$|^\d{1,2}:\d{2}(:\d{2})?[.,]\d{3}\s+-->\s+.*$|^WEBVTT.*$|^NOTE\b.*$")


def _subtitles(text: str) -> str:
    """Drops cue numbers and timings, keeps who said what: `<v Анна>Начнём.` becomes `Анна: Начнём.`"""
    lines = []
    for line in text.splitlines():
        if _TIMING.match(line):
            continue
        line = re.sub(r"<v(?:\.[^ >]*)?\s+([^>]+)>", r"\1: ", line)
        lines.append(re.sub(r"</?[a-z][^>]*>", "", line))
    return "\n".join(lines)
