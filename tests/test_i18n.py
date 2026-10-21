import json
import re
import urllib.request
from pathlib import Path

import pytest

from owa_mail_mcp import i18n
from owa_mail_mcp.config import Settings
from owa_mail_mcp.confirm import ConfirmationBroker
from owa_mail_mcp.models import Outgoing
from owa_mail_mcp.safety import OutgoingReview, PolicyError, check_outgoing

PKG = Path(i18n.__file__).parent
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _load(lang):
    return json.loads((PKG / "locales" / f"{lang}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("lang", [code for code in i18n.LANGUAGES if code != "en"])
def test_every_language_has_every_text_with_the_same_placeholders(lang):
    en, other = _load("en"), _load(lang)
    assert sorted(other) == sorted(en)
    for key, text in en.items():
        assert sorted(PLACEHOLDER.findall(other[key])) == sorted(PLACEHOLDER.findall(text)), key
        assert other[key].strip(), key


def test_every_key_the_code_uses_exists():
    en = _load("en")
    used = set()
    for path in PKG.glob("*.py"):
        used |= set(re.findall(r'\bt\(\s*"([\w.]+)"', path.read_text(encoding="utf-8")))
    for page, prefix in (("setup_page.html", "setup."), ("confirm_page.html", "confirm.")):
        text = (PKG / page).read_text(encoding="utf-8")
        used |= set(re.findall(r"\{\{t:([\w.]+)\}\}", text))
        used |= {k if "." in k else prefix + k for k in re.findall(r'\bT\("([\w.]+)"', text)}
    used = {k for k in used if not k.endswith((".", "_"))}  # prefixes of computed keys
    assert used and not (used - set(en)), sorted(used - set(en))


@pytest.mark.parametrize("raw, code", [
    ("ru_RU.UTF-8", "ru"), ("zh-Hans-CN", "zh"), ("es-419", "es"), ("EN", "en"), ("de_DE", None), ("C", None), ("", None), (None, None),
])
def test_language_codes_are_normalized(raw, code):
    assert i18n.normalize(raw) == code


def test_env_beats_the_saved_choice_which_beats_the_system(monkeypatch):
    monkeypatch.setattr(i18n, "system_language", lambda: "zh")
    monkeypatch.delenv("OWA_MAIL_LANG", raising=False)
    assert i18n.current_language(Settings()) == "zh"
    assert i18n.current_language(Settings(language="es")) == "es"
    monkeypatch.setenv("OWA_MAIL_LANG", "en_US")
    assert i18n.current_language(Settings(language="es")) == "en"


def test_missing_text_falls_back_to_english_and_keeps_unknown_placeholders():
    assert i18n.t("confirm.send", "en") == "Send"
    assert i18n.t("no.such.key", "zh") == "no.such.key"
    assert "{reason}" in i18n.t("msg.net_tls", "es", host="h", port=1)


@pytest.mark.parametrize("lang, word", [("ru", "Отправить"), ("en", "Send"), ("zh", "发送"), ("es", "Enviar")])
def test_confirmation_page_renders_in_each_language(lang, word):
    page = i18n.render_page("confirm_page.html", lang)
    assert f'<html lang="{lang}">' in page and "{{t:" not in page and "{{I18N}}" not in page
    data = json.loads(re.search(r"const I18N = (\{.*?\});\n", page, re.S).group(1))
    assert data["confirm.send"] == word and "policy.gone" in data and "setup.save" not in data


def test_texts_cannot_break_out_of_the_page(monkeypatch):
    monkeypatch.setitem(i18n.catalog("en"), "confirm.h1", "<b>&</b>")
    monkeypatch.setitem(i18n.catalog("en"), "confirm.send", "</script><script>alert(1)</script>")
    page = i18n.render_page("confirm_page.html", "en")
    assert "<h1>&lt;b&gt;&amp;&lt;/b&gt;</h1>" in page
    assert "</script><script>alert(1)" not in page


def test_policy_errors_and_warnings_carry_codes_for_the_window():
    with pytest.raises(PolicyError) as err:
        check_outgoing(Outgoing(to=[f"u{i}@x.ru" for i in range(6)], subject="s", body="b"), my_email="me@x.ru", max_recipients=3)
    assert err.value.code == "too_many" and err.value.params == {"n": 6, "limit": 3}
    assert str(err.value).startswith("Too many recipients")  # the agent still reads English
    review = check_outgoing(Outgoing(to=["me@x.ru"], bcc=["b@x.ru"], subject="s", body="b"), my_email="me@x.ru")
    assert [w.code for w in review.warnings] == ["self", "bcc"]


def test_a_refused_edit_tells_the_window_what_went_wrong():
    broker = ConfirmationBroker(review=lambda m: check_outgoing(m, my_email="me@x.ru"))
    proposal = broker.submit(Outgoing(to=["a@x.ru"], subject="s", body="b"), OutgoingReview(), client="test", sender="me@x.ru", timeout=30)
    error = broker.decide(proposal.id, True, Outgoing(to=["a@x.ru"], subject="", body="b"))
    assert error == "The subject is empty." and error.code == "no_subject"
    assert broker.decide("missing", True).code == "gone"
    assert broker.snapshot()[0]["warning_codes"] == []


def test_setup_window_switches_language_and_keeps_it(tmp_path, monkeypatch):
    from owa_mail_mcp.setup import SetupApp

    monkeypatch.setenv("OWA_MAIL_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("OWA_MAIL_LANG")
    monkeypatch.setattr(i18n, "system_language", lambda: "en")
    app = SetupApp()
    app.start()
    try:
        def get(path=""):
            return urllib.request.urlopen(app.url + path).read().decode()

        def post(path, body):
            req = urllib.request.Request(app.url + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
            return json.loads(urllib.request.urlopen(req).read())

        assert "<title>OWA Mail MCP setup</title>" in get()
        assert json.loads(get("api/state"))["language"] == ""
        assert post("api/language", {"language": "es"}) == {"ok": True}
        assert '<html lang="es">' in get() and "Probar conexión" in get()
        assert json.loads(get("api/state"))["language"] == "es"
        assert Settings.load(with_password=False).language == "es"
        assert post("api/test", {"server": "", "email": ""})["error"] == i18n.t("msg.enter_server_email", "es")
        post("api/language", {"language": ""})  # back to the system language
        assert Settings.load(with_password=False).language is None and '<html lang="en">' in get()
    finally:
        app.stop()


def test_diagnosis_speaks_the_chosen_language():
    from owa_mail_mcp.diagnose import Probe, explain

    probe = Probe(label="EWS", url="https://x/EWS/Exchange.asmx", status=403)
    owa = Probe(label="OWA", url="https://x/owa/", status=200)
    assert explain(probe, None, owa, Settings(), lang="en").startswith("Access to EWS is forbidden")
    assert explain(probe, None, owa, Settings(), lang="ru").startswith("Доступ к EWS запрещён")
