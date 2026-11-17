"""Whether a newer OWA Mail MCP is out, for a notice in the setup and confirmation windows.

Once a day, in the background, the project's release list (docs/releases.json on GitHub: versions and short notes in
each language) is downloaded. Nothing about the user or their mail is sent: it is a plain download of a public file.
On a network that blocks GitHub the check fails quietly and no notice is shown. OWA_MAIL_UPDATE_CHECK=0 turns it off.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from typing import Any

from . import __version__
from .config import config_dir

SOURCE = "https://raw.githubusercontent.com/Khomiakov7706/owa-mail-mcp/main/docs/releases.json"
INSTALL = "curl -LsSf https://raw.githubusercontent.com/Khomiakov7706/owa-mail-mcp/main/install.sh | sh"
EVERY = 24 * 3600
RETRY = 3600
_lock = threading.Lock()
_running = False


def _cache_path():
    return config_dir() / "update_check.json"


def _read_cache() -> dict[str, Any]:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(data: dict[str, Any]) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)  # whole or not at all: a window reading it meanwhile never sees half a file
    except OSError:
        pass


def parse_version(text: str) -> tuple[int, ...] | None:
    m = re.match(r"^\s*(\d+)\.(\d+)(?:\.(\d+))?\s*$", text or "")
    return tuple(int(x or 0) for x in m.groups()) if m else None


def _fetch() -> list[dict[str, Any]] | None:
    request = urllib.request.Request(SOURCE, headers={"User-Agent": f"owa-mail-mcp/{__version__}"})
    with urllib.request.urlopen(request, timeout=5) as resp:
        data = json.loads(resp.read(200_000).decode("utf-8"))
    return clean_releases(data.get("releases") if isinstance(data, dict) else None) or None


def clean_releases(raw: Any) -> list[dict[str, Any]]:
    """Releases in one safe shape, newest first: a version, a date, and up to 8 short plain-text notes per language."""
    out = []
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict) or not parse_version(str(r.get("version") or "")):
            continue
        notes = r.get("notes") if isinstance(r.get("notes"), dict) else {}
        out.append({
            "version": str(r["version"]).strip(),
            "date": str(r.get("date") or "")[:10],
            "notes": {lang: [str(x)[:300] for x in items[:8] if isinstance(x, str)]
                      for lang, items in notes.items() if isinstance(lang, str) and len(lang) <= 5 and isinstance(items, list)},
        })
    return sorted(out, key=lambda r: parse_version(r["version"]), reverse=True)[:20]


def _check_in_background() -> None:
    global _running
    with _lock:
        if _running:
            return
        _running = True

    def run():
        global _running
        try:
            releases = _fetch()
            _write_cache({"checked": time.time(), "releases": releases} if releases else {**_read_cache(), "failed": time.time()})
        except Exception:
            _write_cache({**_read_cache(), "failed": time.time()})
        finally:
            with _lock:
                _running = False

    threading.Thread(target=run, name="owa-mail-mcp-update-check", daemon=True).start()


def enabled() -> bool:
    return os.environ.get("OWA_MAIL_UPDATE_CHECK", "1").strip().lower() not in ("0", "false", "no", "off")


def status(lang: str = "en", *, restart_needed: bool = False) -> dict[str, Any]:
    """What the windows show: a newer version to install with what changed since this one, or an installed one the
    agent has not picked up yet. Never waits for the network: a stale answer starts a check for next time."""
    out: dict[str, Any] = {"current": __version__, "latest": None, "update_available": False, "notes": [],
                           "restart_needed": restart_needed, "install_command": INSTALL}
    if not enabled():
        return out
    cache = _read_cache()
    now = time.time()
    if now - float(cache.get("checked") or 0) > EVERY and now - float(cache.get("failed") or 0) > RETRY:
        _check_in_background()
    releases = clean_releases(cache.get("releases"))
    current = parse_version(__version__)
    if releases and current:
        out["latest"] = releases[0]["version"]
        newer = [r for r in releases if parse_version(r["version"]) > current]
        out["update_available"] = bool(newer)
        out["notes"] = [{"version": r["version"], "date": r["date"], "items": r["notes"].get(lang) or r["notes"].get("en") or []}
                        for r in newer[:3]]
    return out
