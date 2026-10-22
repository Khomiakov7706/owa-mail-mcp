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


# --- meeting text policy ------------------------------------------------------------------------
# A meeting goes to its attendees as the window (or the chat question) shows it. Characters that draw
# nothing can carry any amount of hidden text under a short visible line, so they are refused, except
# the joiners, presentation selectors and tags that emoji and some scripts need. Spaces that only look
# like a plain one become one, and line-end spaces go: the window cannot show either difference.

_DEFAULT_IGNORABLE = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F),
    (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF),
)
_LOOKALIKE_SPACES = {0x00A0, *range(0x2000, 0x200B), 0x202F, 0x205F, 0x3000}


def _ignorable(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in _DEFAULT_IGNORABLE)


def _emoji(cp: int) -> bool:
    return (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or 0x2300 <= cp <= 0x23FF or 0x2B00 <= cp <= 0x2BFF
            or cp in (0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139, 0x3030, 0x303D, 0x3297, 0x3299))


def _uses_joiners(cp: int) -> bool:
    """Scripts that shape with ZWJ/ZWNJ: Arabic and Persian, Indic, Sinhala, Myanmar."""
    return 0x0600 <= cp <= 0x06FF or 0x0750 <= cp <= 0x077F or 0x0900 <= cp <= 0x0DFF or 0x1000 <= cp <= 0x109F


def has_invisible_text(text: str) -> bool:
    """True when the text holds characters that draw nothing outside the places emoji and scripts need them."""
    import unicodedata

    cps = [ord(c) for c in text]
    for i, cp in enumerate(cps):
        prev = cps[i - 1] if i else None
        nxt = cps[i + 1] if i + 1 < len(cps) else None
        if unicodedata.category(chr(cp)) == "Cc":
            if cp in (0x09, 0x0A, 0x0D):
                continue
            return True
        if cp == 0x2800:  # a blank Braille cell looks like a space
            return True
        if not _ignorable(cp):
            continue
        if cp in (0x200C, 0x200D):
            if prev is not None and nxt is not None and _uses_joiners(prev) and _uses_joiners(nxt):
                continue
            if cp == 0x200D and prev is not None and nxt is not None and (_emoji(prev) or prev in (0xFE0F,)) and _emoji(nxt):
                continue
            return True
        if cp in (0xFE0E, 0xFE0F) and prev is not None and _emoji(prev):
            continue
        if 0xE0020 <= cp <= 0xE007F and prev is not None and (prev == 0x1F3F4 or 0xE0020 <= prev <= 0xE007F):
            continue  # subdivision flags: a black flag followed by tag letters
        return True
    return False


def normalize_spaces(text: str) -> str:
    """Look-alike spaces become plain ones, spaces at line ends go."""
    plain = "".join(" " if ord(c) in _LOOKALIKE_SPACES else c for c in text.replace("\r\n", "\n").replace("\r", "\n"))
    return "\n".join(line.rstrip(" \t") for line in plain.split("\n"))


_JOIN_HOSTS = ("teams.microsoft.com", "teams.live.com", "zoom.us", "meet.google.com", "webex.com", "telemost.yandex",
               "ktalk.ru", "jazz.sber.ru", "salutejazz.ru", "vk.com/call", "bbb.", "jitsi", "skype.com", "lync.", "dialin.")


def find_join_url(text: str) -> str | None:
    """The first link to a known meeting service in the invitation (its HTML or the location)."""
    for url in re.findall(r"https?://[^\s\"'<>)]+", text):
        url = url.rstrip(".,;")
        if any(host in url.lower() for host in _JOIN_HOSTS):
            return html.unescape(url)
    return None
