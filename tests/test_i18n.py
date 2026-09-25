"""Interface languages: the catalogs, the choice of language, and the pages that use them.

The catalog tests are the ones that matter over time. A message added in English and forgotten
in a catalog does not break anything visibly — that page simply shows English in the middle of
Turkish — so the check has to be mechanical, and it has to find the messages on its own rather
than from a list someone keeps up to date.
"""
import ast
import importlib
import json
import re
import string
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from timar import i18n, jobs, schedule

ROOT = Path(__file__).parent.parent / "timar"
TEMPLATE_CALL = re.compile(r'_\("((?:[^"\\]|\\.)*)"')
TAG = re.compile(r"</?[a-z]+")


def _message_ids() -> set[str]:
    ids: set[str] = set()
    for path in ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in ("_", "ngettext")):
                count = 2 if node.func.id == "ngettext" else 1
                ids |= {a.value for a in node.args[:count]
                        if isinstance(a, ast.Constant) and isinstance(a.value, str)}
    for path in (ROOT / "web" / "templates").glob("*.html"):
        ids |= set(TEMPLATE_CALL.findall(path.read_text(encoding="utf-8")))
    # Translated where they are displayed, from values rather than literals.
    ids |= set(jobs.TITLES.values()) | set(schedule.KINDS)
    ids |= {day.capitalize() for day in schedule.DAYS}
    ids.add("Report")
    return ids


MESSAGES = _message_ids()
TRANSLATED = [code for code in i18n.LANGUAGES if code != i18n.DEFAULT]


def _catalog(code: str) -> dict[str, str]:
    return json.loads((i18n.LOCALES / f"{code}.json").read_text(encoding="utf-8"))


def _fields(text: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(text) if field}


@pytest.mark.parametrize("code", TRANSLATED)
class TestCatalogs:
    def test_every_message_is_translated(self, code):
        missing = MESSAGES - _catalog(code).keys()
        assert not missing, f"{code}.json is missing {sorted(missing)}"

    def test_no_stale_entries(self, code):
        # A message reworded in English leaves its old translation behind, which then quietly
        # stops matching anything. Caught here instead of accumulating.
        stale = _catalog(code).keys() - MESSAGES
        assert not stale, f"{code}.json has entries nothing uses: {sorted(stale)}"

    def test_placeholders_survive(self, code):
        # A dropped `{name}` renders a sentence about nobody; an extra one raises KeyError.
        wrong = {k: v for k, v in _catalog(code).items() if _fields(k) != _fields(v)}
        assert not wrong

    def test_markup_survives(self, code):
        wrong = {k: v for k, v in _catalog(code).items()
                 if sorted(TAG.findall(k)) != sorted(TAG.findall(v))}
        assert not wrong

    def test_no_straight_double_quotes(self, code):
        # Translations land inside double-quoted HTML attributes (hx-confirm, placeholder, title)
        # unescaped, because they are trusted markup. One `"` would end the attribute early.
        assert not [v for v in _catalog(code).values() if '"' in v]


class TestNegotiation:
    def test_cookie_wins(self):
        assert i18n.negotiate("tr", "de-DE,de;q=0.9") == "tr"

    def test_unknown_cookie_falls_through_to_the_browser(self):
        assert i18n.negotiate("xx", "fr-FR,fr;q=0.9") == "fr"

    def test_quality_order_is_respected(self):
        assert i18n.negotiate(None, "pt-BR;q=0.9,ja;q=0.8,ru;q=0.5") == "ja"
        assert i18n.negotiate(None, "ru;q=0.5,zh-CN;q=0.7") == "zh"

    def test_english_when_nothing_matches(self):
        assert i18n.negotiate(None, "pt-BR,es;q=0.9") == "en"
        assert i18n.negotiate(None, None) == "en"
        assert i18n.negotiate(None, "tr;q=abc") == "en"

    def test_outside_a_request_everything_is_english(self):
        # The scheduler, reports and Telegram never set a language, and must not inherit one.
        assert i18n.current() == "en"
        assert schedule.Schedule(enabled=False).describe() == "not scheduled"


def test_values_are_escaped_but_the_sentence_is_not():
    i18n.activate("tr")
    try:
        out = i18n._markup("Enrol {name}", name="<b>x</b>")
    finally:
        i18n.activate("en")
    assert out == "&lt;b&gt;x&lt;/b&gt; kaydı"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TIMAR_DATA", str(tmp_path))
    from timar import config
    from timar.web import app as app_module, auth
    importlib.reload(config)
    importlib.reload(auth)
    importlib.reload(app_module)
    auth._failures.clear()
    return TestClient(app_module.app, follow_redirects=False)


class TestPages:
    def test_setup_follows_the_browser_language(self, client):
        body = client.get("/setup", headers={"Accept-Language": "tr-TR,tr;q=0.9"}).text
        assert '<html lang="tr">' in body
        assert "Timar'ı kur" in body

    def test_language_switch_is_open_before_setup(self, client):
        # The person who cannot read the setup page is the one who needs the switch most.
        response = client.get("/lang", params={"code": "ja", "next": "/setup"})
        assert response.status_code == 303
        assert response.headers["location"] == "/setup"
        assert response.cookies.get(i18n.COOKIE) == "ja"
        assert "Timar のセットアップ" in client.get("/setup").text

    @pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example/", "/\\evil"])
    def test_switch_is_not_an_open_redirect(self, client, target):
        response = client.get("/lang", params={"code": "de", "next": target})
        assert response.headers["location"] == "/"

    def test_unknown_language_sets_nothing(self, client):
        response = client.get("/lang", params={"code": "xx", "next": "/setup"})
        assert i18n.COOKIE not in response.cookies

    @pytest.mark.parametrize("code", list(i18n.LANGUAGES))
    def test_every_page_renders_in_every_language(self, client, code):
        """Catches a translation whose placeholders make `format` raise at render time."""
        client.cookies.set(i18n.COOKIE, code)
        client.post("/setup", data={"username": "op", "password": "correct-horse-battery"})
        from timar import config
        config.save({"servers": [
            {"name": "hv", "host": "10.0.0.2", "user": "root", "platform": "proxmox",
             "wol_mac": "aa:bb:cc:dd:ee:01", "manages_vms": [{"vm_id": 101, "server_name": "vm"}]},
            {"name": "vm", "host": "10.0.0.3", "user": "op"},
        ]})
        for path in ("/", "/reports", "/settings", "/settings?tab=global",
                     "/settings?edit=vm", "/settings?enroll=vm", "/fragments/jobs"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert f'<html lang="{code}">' in response.text or path.startswith("/fragments")

    @pytest.mark.parametrize("path", ["/setup", "/login", "/", "/settings", "/reports"])
    def test_the_switch_is_in_the_header(self, client, path):
        """At the foot of the page it sat below the jobs table on the dashboard and went unfound."""
        if path != "/setup":
            client.post("/setup", data={"username": "op", "password": "correct-horse-battery"})
        if path == "/login":
            client.cookies.clear()
        body = client.get(path).text
        header = body[body.index("<header>"):body.index("</header>")]
        assert 'action="/lang"' in header
        assert body.count('action="/lang"') == 1

    def test_form_errors_come_back_translated(self, client):
        client.cookies.set(i18n.COOKIE, "de")
        client.post("/setup", data={"username": "op", "password": "correct-horse-battery"})
        body = client.post("/settings/servers", data={"name": "", "host": "", "user": "",
                                                      "platform": "linux"}).text
        assert "Name ist erforderlich." in body
