"""Files the agent attaches to an outgoing email, checked before the user is asked.

An agent that reads other people's email can be talked into sending the user's files somewhere. So only common
document and image types go, each file's content must really be of the type its name says (a key or a password file
renamed to .pdf is refused), nothing from hidden or system folders, within size limits. The file is read once, here:
what the window shows is what is sent, even if the file changes on disk afterwards.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from .text import contains_bidi_controls, has_invisible_text

MAX_FILES = 10
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 25 * 1024 * 1024
"""Exchange usually refuses messages over 25–35 MB, and attachments grow by a third in transit."""

_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".doc": "application/msword",
    ".xls": "application/vnd.ms-excel",
    ".ppt": "application/vnd.ms-powerpoint",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".rtf": "application/rtf",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".md": "text/markdown",
}
_OOXML_PART = {".docx": "word/", ".xlsx": "xl/", ".pptx": "ppt/"}


class AttachmentError(ValueError):
    pass


@dataclass
class OutgoingFile:
    name: str
    content_type: str
    data: bytes
    path: str

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def to_dict(self) -> dict:
        return {"name": self.name, "size": self.size, "content_type": self.content_type, "path": self.path}


def load_files(paths: list[str]) -> list[OutgoingFile]:
    """Every path checked and read, or AttachmentError naming the first that cannot go."""
    if len(paths) > MAX_FILES:
        raise AttachmentError(f"At most {MAX_FILES} attachments.")
    files = [load_file(p) for p in paths]
    names = [f.name.lower() for f in files]
    if len(set(names)) != len(names):
        raise AttachmentError("Two attachments have the same name; rename one of them.")
    if sum(f.size for f in files) > MAX_TOTAL_BYTES:
        raise AttachmentError(f"Attachments together are larger than {MAX_TOTAL_BYTES // (1024 * 1024)} MB.")
    return files


def load_file(raw_path: str) -> OutgoingFile:
    shown = str(raw_path).strip()
    path = Path(os.path.expanduser(shown))
    if not path.is_absolute():
        raise AttachmentError(f"{shown!r}: give the full path to the file (it is read on the user's computer).")
    name = path.name
    if has_invisible_text(name) or contains_bidi_controls(name):
        raise AttachmentError(f"{shown!r}: the file name contains invisible or text-direction characters.")
    ext = path.suffix.lower()
    if ext not in TYPES:
        allowed = ", ".join(sorted(TYPES))
        raise AttachmentError(f"{name}: this type of file cannot be attached. Allowed: {allowed}.")
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise AttachmentError(f"{shown}: no such file.") from None
    _check_place(real, shown)
    if not real.is_file():
        raise AttachmentError(f"{shown}: not a regular file.")
    if real.suffix.lower() != ext:
        raise AttachmentError(f"{shown}: a link to a file of another type.")
    size = real.stat().st_size
    if size > MAX_FILE_BYTES:
        raise AttachmentError(f"{name} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
    if size == 0:
        raise AttachmentError(f"{name} is empty.")
    data = real.read_bytes()
    if not _content_matches(ext, data):
        raise AttachmentError(f"{name}: the content is not a real {ext[1:].upper()} file, so it is not attached.")
    return OutgoingFile(name=name, content_type=TYPES[ext], data=data, path=str(real))


def _roots() -> list[Path]:
    """Where attachments may come from: the user's home folder, temporary folders, external drives."""
    roots = [Path.home(), Path(tempfile.gettempdir()), Path("/tmp")]
    if sys.platform == "darwin":
        roots += [Path("/Volumes"), Path("/private/tmp"), Path("/private/var/folders")]
    elif sys.platform.startswith("linux"):
        roots += [Path("/media"), Path("/mnt"), Path("/run/media")]
    out = []
    for r in roots:
        try:
            out.append(r.resolve())
        except OSError:
            continue
    return out


def _check_place(real: Path, shown: str) -> None:
    home = Path.home().resolve()
    for root in _roots():
        if real == root or root not in real.parents:
            continue
        inside = real.relative_to(root).parts
        if any(part.startswith(".") for part in inside):
            raise AttachmentError(f"{shown}: files from hidden folders (settings, keys, caches) are not attached.")
        if root == home and inside and inside[0] in ("Library", "AppData") and not (
            len(inside) > 1 and inside[0] == "Library" and inside[1] in ("Mobile Documents", "CloudStorage")
        ):
            raise AttachmentError(f"{shown}: files from system and application folders are not attached.")
        return
    raise AttachmentError(f"{shown}: only files from your home folder, temporary folders or external drives are attached.")


def _content_matches(ext: str, data: bytes) -> bool:
    head = data[:16]
    if ext == ".pdf":
        return data[:1024].lstrip().startswith(b"%PDF-")
    if ext == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in (".jpg", ".jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if ext == ".gif":
        return head[:6] in (b"GIF87a", b"GIF89a")
    if ext == ".webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if ext == ".heic":
        return head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"hevc", b"heim", b"heis")
    if ext in (".doc", ".xls", ".ppt"):
        return head.startswith(_OLE)
    if ext == ".rtf":
        return data.lstrip()[:5] == b"{\\rtf"
    if ext in _OOXML_PART or ext in (".odt", ".ods", ".odp"):
        return _zip_document(ext, data)
    if ext in (".csv", ".txt", ".md"):
        return _text(data) and not _looks_like_secret(data)
    return False


def _looks_like_secret(data: bytes) -> bool:
    """A private key saved as a .txt is not a document."""
    head = data[:200_000]
    return b"PRIVATE KEY-----" in head or b"PRIVATE KEY BLOCK-----" in head or b"OPENSSH PRIVATE KEY" in head


def _zip_document(ext: str, data: bytes) -> bool:
    if not data.startswith(b"PK\x03\x04"):
        return False
    try:
        with zipfile.ZipFile(BytesIO(data)) as z:
            names = z.namelist()
            if ext in _OOXML_PART:
                return "[Content_Types].xml" in names and any(n.startswith(_OOXML_PART[ext]) for n in names)
            return "mimetype" in names and z.read("mimetype").decode("ascii", "replace").strip() == TYPES[ext]
    except (zipfile.BadZipFile, OSError, KeyError):
        return False


def _text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            data.decode(encoding)
            return True
        except UnicodeDecodeError:
            continue
    return False

