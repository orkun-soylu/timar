"""Static URLs carry a hash of the file: a changed sprite is a new URL, never a stale cache hit.

The bug this guards: two logos were added to the sprite, and a browser kept the old sprite (the
files carry no Cache-Control). `<use href="…#tplink">` on a sprite without that symbol draws
nothing and reports nothing, so the two rows stood with an empty slot where the logo belongs.
"""
import re

from tests.test_web import client  # noqa: F401 — the fixture

from timar.web import assets


def test_the_url_names_the_file_and_its_content(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "STATIC", tmp_path)
    (tmp_path / "a.svg").write_text("one")
    assets.static_url.cache_clear()
    first = assets.static_url("a.svg")
    assert re.fullmatch(r"/static/a\.svg\?v=[0-9a-f]{10}", first)
    (tmp_path / "a.svg").write_text("two")
    assets.static_url.cache_clear()
    assert assets.static_url("a.svg") != first
    assets.static_url.cache_clear()


def test_the_pages_use_it(client):
    from tests.test_web import complete_setup
    complete_setup(client)
    page = client.get("/").text
    assert re.search(r'src="/static/htmx\.min\.js\?v=[0-9a-f]{10}"', page)
