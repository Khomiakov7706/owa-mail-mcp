"""Formatted email: a small Markdown subset that the server itself turns into HTML.

The agent never supplies HTML. Everything it writes is escaped first, and only a fixed set of tags comes out:
paragraphs, line breaks, bold, italic, strikethrough, inline code, headings, lists and links (http, https, mailto).
No images, styles, scripts, forms or remote content, so the HTML can neither hide text from the user's review
nor track or attack the recipient.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

_FONT = "font-family: Calibri, Arial, Helvetica, sans-serif; font-size: 11pt;"
_LINK = re.compile(r"\[([^\[\]\n]{1,500})\]\(((?:https?://|mailto:)[^\s()<>]{1,2000})\)")
_BARE_URL = re.compile(r"(?<![\w/\"'=])(https?://[^\s<>()\"']{1,2000})")
_CODE = re.compile(r"`([^`\n]{1,500})`")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])|(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*$")
_BULLET = re.compile(r"^\s{0,3}[-*•]\s+(.+)$")
_NUMBER = re.compile(r"^\s{0,3}\d{1,3}[.)]\s+(.+)$")
_DOMAIN_LIKE = re.compile(r"(?:https?://)?((?:[\w-]+\.)+[a-z]{2,})(?:[/:?#]\S*)?$", re.IGNORECASE)


def to_html(text: str, *, preview: bool = False) -> str:
    """The email's HTML. With `preview`, for the confirmation window: every link shows where it really goes."""
    blocks = _blocks(text.replace("\r\n", "\n").replace("\r", "\n"))
    body = "\n".join(_block(kind, lines, preview) for kind, lines in blocks)
    return body if preview else f'<html><body><div style="{_FONT}">\n{body}\n</div></body></html>'


def to_plain(text: str) -> str:
    """The same message without the markup: links as `text (url)`. For the text part of drafts and logs."""
    text = _LINK.sub(lambda m: m.group(1) if m.group(1) == m.group(2) else f"{m.group(1)} ({m.group(2)})", text)
    text = _BOLD.sub(lambda m: m.group(1) or m.group(2), text)
    text = _STRIKE.sub(r"\1", text)
    text = _ITALIC.sub(lambda m: m.group(1) or m.group(2), text)
    return _CODE.sub(r"\1", text)


def misleading_links(text: str) -> list[str]:
    """Links whose visible text looks like one address while they lead to another site."""
    out = []
    for label, url in _LINK.findall(text):
        shown = _DOMAIN_LIKE.match(label.strip())
        if not shown or url.lower().startswith("mailto:"):
            continue
        target = (urlsplit(url).hostname or "").lower()
        claimed = shown.group(1).lower()
        if target != claimed and not target.endswith("." + claimed):
            out.append(f"{label} → {url}")
    return out


def _blocks(text: str) -> list[tuple[str, list[str]]]:
    blocks: list[tuple[str, list[str]]] = []
    for line in text.split("\n"):
        if not line.strip():
            blocks.append(("gap", []))
            continue
        kind = "h" if _HEADING.match(line) else "ul" if _BULLET.match(line) else "ol" if _NUMBER.match(line) else "p"
        if kind == "h":
            blocks.append(("h", [line]))
        elif blocks and blocks[-1][0] == kind and kind != "h":
            blocks[-1][1].append(line)
        else:
            blocks.append((kind, [line]))
    return [b for b in blocks if b[0] != "gap"]


def _block(kind: str, lines: list[str], preview: bool) -> str:
    if kind == "h":
        m = _HEADING.match(lines[0])
        level = len(m.group(1)) + 2  # # → h3: an email heading, not a page title
        return f"<h{level}>{_inline(m.group(2), preview)}</h{level}>"
    if kind in ("ul", "ol"):
        pattern = _BULLET if kind == "ul" else _NUMBER
        items = "".join(f"<li>{_inline(pattern.match(x).group(1), preview)}</li>" for x in lines)
        return f"<{kind}>{items}</{kind}>"
    return "<p>" + "<br>\n".join(_inline(x, preview) for x in lines) + "</p>"


def _inline(text: str, preview: bool) -> str:
    """Escapes everything, then turns the markup into tags. Placeholders keep code and links from being reformatted."""
    kept: list[str] = []

    def keep(fragment: str) -> str:
        kept.append(fragment)
        return f"\x00{len(kept) - 1}\x00"

    text = text.replace("\x00", "")
    text = _CODE.sub(lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = _LINK.sub(lambda m: keep(_anchor(m.group(2), _emphasis(html.escape(m.group(1))), preview, m.group(1))), text)
    text = _BARE_URL.sub(_bare_link(keep), text)
    text = _emphasis(html.escape(text))
    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], text)


def _bare_link(keep):
    def link(m: re.Match) -> str:
        url = m.group(1).rstrip(".,;:!?")  # the sentence's full stop is not part of the address
        return keep(_anchor(url, html.escape(url), False, url)) + m.group(1)[len(url):]
    return link


def _emphasis(escaped: str) -> str:
    escaped = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", escaped)
    escaped = _STRIKE.sub(r"<s>\1</s>", escaped)
    return _ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", escaped)


def _anchor(url: str, label_html: str, preview: bool, label: str) -> str:
    href = html.escape(url, quote=True)
    link = f'<a href="{href}">{label_html}</a>'
    if preview and label != url:
        link += f' <span class="url">({html.escape(url)})</span>'
    return link
