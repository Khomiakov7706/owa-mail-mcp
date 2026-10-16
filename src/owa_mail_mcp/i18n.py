"""Interface languages for the windows, the messages they show and the command line.

Texts live in `locales/<lang>.json`; a key missing in one language falls back to English. The
language is OWA_MAIL_LANG, else the one picked in the setup window, else the system's, else English.
What the agent reads (tool descriptions, tool errors) stays in English on purpose.
"""

from __future__ import annotations

import html
import json
import locale
import os
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

LANGUAGES = {"ru": "Русский", "en": "English", "zh": "中文", "es": "Español"}
DEFAULT = "en"
_LOCALES = Path(__file__).parent / "locales"


class Coded(str):
    """English text that also carries a message key and its values, so a window can show it in
    the user's language while logs and the agent keep the English."""

    code: str
    params: dict[str, Any]

    def __new__(cls, text: str, code: str, **params: Any) -> "Coded":
        obj = super().__new__(cls, text)
        obj.code, obj.params = code, params
        return obj


def normalize(value: str | None) -> str | None:
    """`ru_RU.UTF-8`, `zh-Hans-CN`, `es-419`, `EN` → a supported code, or None."""
    code = re.split(r"[-_.@]", (value or "").strip().lower())[0]
    return code if code in LANGUAGES else None


@lru_cache(maxsize=1)
def system_language() -> str:
    env = os.environ
    for var in ("LC_ALL", "LC_MESSAGES", "LANGUAGE"):
        if normalize(env.get(var, "").split(":")[0]):
            return normalize(env[var].split(":")[0])  # type: ignore[return-value]
    for candidate in _platform_languages():
        if normalize(candidate):
            return normalize(candidate)  # type: ignore[return-value]
    if normalize(env.get("LANG")):
        return normalize(env["LANG"])  # type: ignore[return-value]
    try:
        found = normalize(locale.getlocale()[0])
    except ValueError:
        found = None
    return found or DEFAULT


def _platform_languages() -> list[str]:
    """The UI language the user picked in the OS, which a GUI-started process may not get in LANG."""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["defaults", "read", "-g", "AppleLanguages"], capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
        return re.findall(r'"?([A-Za-z]{2}[-\w]*)"?', out)[:1]
    if sys.platform == "win32":
        try:
            import ctypes

            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()  # type: ignore[attr-defined]
            return [locale.windows_locale.get(lang_id, "")]
        except Exception:
            return []
    return []


def current_language(settings: Any = None) -> str:
    """OWA_MAIL_LANG, then the saved choice, then the system language."""
    if normalize(os.environ.get("OWA_MAIL_LANG")):
        return normalize(os.environ["OWA_MAIL_LANG"])  # type: ignore[return-value]
    if settings is None:
        from .config import Settings

        settings = Settings.load(with_password=False)
    return normalize(getattr(settings, "language", None)) or system_language()


@lru_cache(maxsize=None)
def catalog(lang: str) -> dict[str, str]:
    def read(code: str) -> dict[str, str]:
        try:
            return json.loads((_LOCALES / f"{code}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    base = read(DEFAULT)
    return base if lang == DEFAULT else {**base, **read(lang)}


class _Keep(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def t(key: str, lang: str | None = None, **params: Any) -> str:
    text = catalog(lang or current_language()).get(key, key)
    return text.format_map(_Keep(params)) if params else text


def render_page(name: str, lang: str) -> str:
    """A window page with `{{t:key}}` filled in (HTML-escaped), `{{LANG}}`, the language menu and
    the texts its script needs as `{{I18N}}` (every key under the page's prefix, plus `policy.`)."""
    page = (Path(__file__).parent / name).read_text(encoding="utf-8")
    texts = catalog(lang)
    prefix = name.split("_")[0] + "."
    for_script = {k: v for k, v in texts.items() if k.startswith((prefix, "policy."))}
    script_json = json.dumps(for_script, ensure_ascii=False).replace("</", "<\\/")
    page = re.sub(r"\{\{t:([\w.]+)\}\}", lambda m: html.escape(texts.get(m.group(1), m.group(1))), page)
    return page.replace("{{LANG}}", lang).replace("{{I18N}}", script_json)
