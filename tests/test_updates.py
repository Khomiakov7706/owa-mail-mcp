"""Telling the user about a new version, with what changed, without slowing anything down or leaking anything."""

import json
import time
from pathlib import Path

import pytest

from owa_mail_mcp import updates

RELEASES = {"releases": [
    {"version": "99.1.0", "date": "2099-01-02", "notes": {"ru": ["Новое «А»"], "en": ["New A"]}},
    {"version": "99.0.0", "date": "2099-01-01", "notes": {"en": ["New B", 5, "<script>x</script>"]}},
    {"version": "0.0.1", "notes": {"en": ["Old"]}},
    {"version": "not a version"},
]}


@pytest.fixture
def check(monkeypatch, tmp_path):
    monkeypatch.setenv("OWA_MAIL_UPDATE_CHECK", "1")
    monkeypatch.setattr(updates, "config_dir", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(updates, "_fetch", lambda: calls.append(1) or updates.clean_releases(RELEASES["releases"]))
    return calls


def _wait_for_cache(tmp_path: Path):
    for _ in range(100):
        if (tmp_path / "update_check.json").exists():
            return
        time.sleep(0.02)


def test_first_look_starts_a_check_and_the_next_shows_the_notes(check, tmp_path):
    first = updates.status("ru")
    assert first["update_available"] is False  # never waits for the network
    _wait_for_cache(tmp_path)
    later = updates.status("ru")
    assert later["update_available"] is True and later["latest"] == "99.1.0"
    assert later["notes"] == [{"version": "99.1.0", "date": "2099-01-02", "items": ["Новое «А»"]},
                              {"version": "99.0.0", "date": "2099-01-01", "items": ["New B", "<script>x</script>"]}]
    assert "install.sh" in later["install_command"]
    updates.status("ru")
    assert len(check) == 1  # once a day, not on every look


def test_offline_or_turned_off_shows_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(updates, "config_dir", lambda: tmp_path)
    monkeypatch.setenv("OWA_MAIL_UPDATE_CHECK", "1")

    def blocked():
        raise OSError("blocked by the proxy")

    monkeypatch.setattr(updates, "_fetch", blocked)
    updates.status()
    _wait_for_cache(tmp_path)
    assert updates.status()["update_available"] is False
    assert "failed" in json.loads((tmp_path / "update_check.json").read_text())
    monkeypatch.setenv("OWA_MAIL_UPDATE_CHECK", "0")
    (tmp_path / "update_check.json").write_text(json.dumps({"checked": time.time(), "releases": RELEASES["releases"]}))
    assert updates.status()["update_available"] is False


def test_the_published_release_list_is_valid_and_has_this_version():
    from owa_mail_mcp import __version__

    data = json.loads((Path(__file__).parent.parent / "docs" / "releases.json").read_text())
    releases = updates.clean_releases(data["releases"])
    assert releases[0]["version"] == __version__
    for r in releases:
        assert set(r["notes"]) == {"ru", "en", "zh", "es"} and all(r["notes"].values())


def test_windows_carry_the_update(make_server, monkeypatch):
    from test_server import _request

    monkeypatch.setattr(updates, "status", lambda lang="en", restart_needed=False: {"update_available": True, "latest": "99.1.0",
                                                                                     "lang": lang, "restart_needed": restart_needed})
    _, tools = make_server()
    _, data = _request(tools.ui, "api/pending")
    assert data["update"]["latest"] == "99.1.0" and data["update"]["lang"] == "ru"
