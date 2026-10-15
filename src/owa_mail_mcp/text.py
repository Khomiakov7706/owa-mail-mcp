"""Text handling: HTML to plain text, cutting quoted history, and what an outgoing message may contain."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

# --- HTML -> text -------------------------------------------------------------------------------

_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote", "hr"}
_SKIP_TAGS = {"script", "style", "head", "title"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
            if tag == "li":
                self.parts.append("- ")
        elif tag == "td":
            self.parts.append("\t")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS and tag not in {"li", "br", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(source: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(source)
        parser.close()
        text = "".join(parser.parts)
    except Exception:  # malformed HTML: fall back to stripping tags
        text = html.unescape(re.sub(r"<[^>]+>", " ", source))
    return normalize_whitespace(text)


def normalize_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip()


# --- quoted history -----------------------------------------------------------------------------

_QUOTE_MARKERS = [
    re.compile(r"^-{2,}\s*(original message|исходное сообщение|пересылаемое сообщение|forwarded message)\s*-{2,}", re.I),
    re.compile(r"^_{10,}$"),  # Outlook's separator line before the From:/Sent: block
    re.compile(r"^(from|от|отправлено|sent)\s*:\s*.+", re.I),
    re.compile(r"^on .{4,200} wrote:$", re.I),
    re.compile(r"^.{4,200}\s(написал|написала|написал\(а\)|пишет)\s*:$", re.I),
]


def strip_quoted(text: str) -> tuple[str, bool]:
    """The new part of a reply, without the history quoted below it. Returns (text, had_quote).

    Heuristic: cut at the first reply header (Outlook's From:/От: block, "On ... wrote:") or at the
    first run of `>` lines. A header on the very first line is kept, since then it is the content.
    """
    lines = text.split("\n")
    for index, line in enumerate(lines):
        stripped = line.strip()
        if index == 0:
            continue
        if stripped.startswith(">") and all(l.strip().startswith(">") or not l.strip() for l in lines[index:]):
            return "\n".join(lines[:index]).strip(), True
        if any(p.match(stripped) for p in _QUOTE_MARKERS):
            # "From:" alone is too common in prose: require a Sent/Date/To line close below it.
            if stripped.lower().startswith(("from", "от", "sent", "отправлено")) and not _looks_like_header_block(lines, index):
                continue
            return "\n".join(lines[:index]).strip(), True
    return text.strip(), False


def _looks_like_header_block(lines: list[str], start: int) -> bool:
    window = [l.strip().lower() for l in lines[start + 1 : start + 5]]
    keys = ("sent:", "date:", "to:", "subject:", "отправлено:", "дата:", "кому:", "тема:", "from:", "от:")
    return sum(1 for l in window if l.startswith(keys)) >= 1


def truncate(text: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + " …", True


def preview(text: str, limit: int = 200) -> str:
    return truncate(" ".join(text.split()), limit)[0]


# --- outgoing text policy -----------------------------------------------------------------------
# The arguments of send_email come from a model that may have been steered by text someone else
# wrote (an email it just read). The confirmation window is the user's only look at the message,
# so text that renders differently from what it is gets refused or flagged.

_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


def contains_bidi_controls(text: str) -> bool:
    """U+202A–U+202E and U+2066–U+2069 reorder what is displayed: `moc.live@ivan` shows as `ivan@evil.com`."""
    return any(0x202A <= ord(c) <= 0x202E or 0x2066 <= ord(c) <= 0x2069 for c in text)


def contains_hidden_characters(text: str) -> bool:
    """Zero-width spaces, BOM, soft hyphens and the like. ZWJ/ZWNJ are fine (emoji, scripts)."""
    import unicodedata

    for c in text:
        if c in "‌‍":
            continue
        if unicodedata.category(c) == "Cf":
            return True
    return False


def is_valid_address(email: str) -> bool:
    """Plain ASCII only: a Cyrillic look-alike letter in a domain is invisible otherwise."""
    return bool(_EMAIL_RE.match(email)) and len(email) <= 254


def normalize_body(body: str) -> str:
    """Collapse runs of blank lines, so a gap cannot push the tail of a message out of view."""
    text = body.replace("\r\n", "\n").replace("\r", "\n").replace(" ", "\n").replace(" ", "\n")
    out: list[str] = []
    for line in text.split("\n"):
        blank = not line.strip()
        if blank and (not out or not out[-1].strip()):
            continue
        out.append("" if blank else line.rstrip())
    return "\n".join(out).strip()


def domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""
