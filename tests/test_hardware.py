"""The hardware pages: Markdown documents in `hardware/`, read as pages, edited on request."""
import importlib

import pytest
from fastapi.testclient import TestClient

from timar import hardware

PASSWORD = "correct-horse-battery"


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("TIMAR_DATA", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(data):
    from timar import config
    from timar.web import app as app_module, auth
    importlib.reload(config)
    importlib.reload(auth)
    importlib.reload(app_module)
    auth._failures.clear()
    c = TestClient(app_module.app, follow_redirects=False)
    c.post("/setup", data={"username": "op", "password": PASSWORD})
    return c


class TestRender:
    def test_raw_html_shows_as_text(self):
        """An uploaded file must not run script in a page that holds the fleet's SSH key."""
        out = hardware.render('<script>alert(1)</script>\n\nhi <img src=x onerror="alert(1)">')
        assert "<script" not in out and "<img" not in out
        assert "&lt;script&gt;" in out

    @pytest.mark.parametrize("target", [
        "javascript:alert(1)", "JavaScript:alert(1)", "&#106;avascript:alert(1)",
        "&#x6A;avascript:alert(1)", "java&Tab;script:alert(1)", "data:text/html,x", "//evil.example",
    ])
    def test_a_link_cannot_leave_for_a_script(self, target):
        out = hardware.render(f"[x]({target}) ![y]({target})")
        assert out.count('="#"') == 2, out

    @pytest.mark.parametrize("target", [
        "https://example.com/a", "http://10.0.0.1", "mailto:a@b.c", "#section", "/hardware/x",
        "rack-2", "rack-2#top",
    ])
    def test_ordinary_links_survive(self, target):
        assert f'href="{target}"' in hardware.render(f"[x]({target})")

    def test_front_matter_is_not_shown_and_names_the_document(self, data):
        hardware.save("inv", "---\ntitle: Inventory\nupdated_by: claude\n---\n# Machines\n", None)
        assert hardware.get("inv")["title"] == "Inventory"
        out = hardware.render(hardware.get("inv")["text"])
        assert "updated_by" not in out and "<h1" in out

    def test_tables_scroll_in_their_own_box(self):
        out = hardware.render("|a|b|\n|-|-|\n|1|2|\n")
        assert '<div class="table-scroll"><table>' in out


class TestStore:
    def test_title_falls_back_to_the_first_heading_then_the_name(self, data):
        hardware.save("a", "# Rack one\n", None)
        hardware.save("b", "no heading\n", None)
        assert [d["title"] for d in hardware.listing()] == ["b", "Rack one"]

    def test_order_puts_documents_first_then_titles(self, data):
        hardware.save("notes", "---\ntitle: AI notes\norder: 2\n---\n", None)
        hardware.save("hw", "---\ntitle: Hardware\norder: 1\n---\n", None)
        hardware.save("extra", "# Aardvark\n", None)
        hardware.save("bad", "---\ntitle: Bad order\norder: soon\n---\n", None)
        assert [d["slug"] for d in hardware.listing()] == ["hw", "notes", "extra", "bad"]

    def test_a_save_over_someone_elses_change_is_refused(self, data):
        hardware.save("inv", "one\n", None)
        base = hardware.get("inv")["digest"]
        (data / "hardware" / "inv.md").write_text("changed by a script\n")
        with pytest.raises(hardware.Conflict):
            hardware.save("inv", "mine\n", base)
        assert (data / "hardware" / "inv.md").read_text() == "changed by a script\n"

    def test_a_new_document_does_not_replace_one(self, data):
        hardware.save("inv", "one\n", None)
        with pytest.raises(hardware.Conflict):
            hardware.save("inv", "two\n", None)

    def test_line_endings_from_a_textarea_are_normalised(self, data):
        hardware.save("inv", "a\r\nb\r\n", None)
        assert (data / "hardware" / "inv.md").read_bytes() == b"a\nb\n"

    @pytest.mark.parametrize("slug", ["../config", "Inv", "", "a/b", "-x"])
    def test_names_cannot_leave_the_directory(self, data, slug):
        with pytest.raises(hardware.HardwareError):
            hardware.save(slug, "x", None)

    @pytest.mark.parametrize("name, slug", [
        ("Inventory.md", "inventory"), ("Rack 2", "rack-2"), ("C:\\x\\inv (2).md", "inv-2"),
        ("../../etc/passwd", "passwd"),
    ])
    def test_slugify(self, name, slug):
        assert hardware.slugify(name) == slug


class TestPages:
    def test_signed_out_it_is_closed(self, client):
        client.cookies.clear()
        assert client.get("/hardware").headers["location"] == "/login"
        assert client.post("/hardware", data={"name": "x"}).headers["location"] == "/login"

    def test_upload_view_edit_delete(self, client, data):
        response = client.post("/hardware", files={"file": ("Inventory.md", b"# Machines\n\n- box\n")})
        assert response.headers["location"] == "/hardware/inventory"
        page = client.get("/hardware/inventory").text
        assert "<li>box</li>" in page and 'href="/hardware/inventory/edit"' in page
        assert "Machines" in client.get("/hardware").text

        editor = client.get("/hardware/inventory/edit").text
        base = hardware.digest("# Machines\n\n- box\n")
        assert f'value="{base}"' in editor
        response = client.post("/hardware/inventory", data={"text": "# Machines\n\n- two\n", "base": base})
        assert response.headers["location"] == "/hardware/inventory"
        assert "<li>two</li>" in client.get("/hardware/inventory").text

        assert client.post("/hardware/inventory/delete").headers["location"] == "/hardware"
        assert not (data / "hardware" / "inventory.md").exists()
        assert client.get("/hardware/inventory").status_code == 404

    def test_the_page_opens_the_first_tab(self, client):
        hardware.save("notes", "---\ntitle: AI notes\norder: 2\n---\nsecret detail\n", None)
        hardware.save("hw", "---\ntitle: Hardware\norder: 1\n---\n- board\n", None)
        page = client.get("/hardware").text
        assert "<li>board</li>" in page and "secret detail" not in page
        assert page.index('href="/hardware/hw"') < page.index('href="/hardware/notes"')
        assert 'href="/hardware/hw" class="selected"' in page
        other = client.get("/hardware/notes").text
        assert 'href="/hardware/notes" class="selected"' in other and "secret detail" in other

    def test_an_empty_page_offers_the_add_button(self, client):
        page = client.get("/hardware").text
        assert 'popovertarget="hardware-add"' in page and 'class="tabs"' not in page

    def test_an_empty_document_opens_for_editing(self, client):
        response = client.post("/hardware", data={"name": "Rack 2"})
        assert response.headers["location"] == "/hardware/rack-2/edit"
        assert hardware.get("rack-2")["text"] == "# Rack 2\n"

    def test_a_conflict_keeps_what_was_typed(self, client, data):
        hardware.save("inv", "one\n", None)
        base = hardware.digest("one\n")
        (data / "hardware" / "inv.md").write_text("two\n")
        response = client.post("/hardware/inv", data={"text": "my edit", "base": base})
        assert response.status_code == 409
        assert "my edit" in response.text
        # The next save is a deliberate overwrite of what is there now.
        assert f'value="{hardware.digest("two" + chr(10))}"' in response.text

    def test_rejected_adds_say_why(self, client):
        assert client.post("/hardware", data={"name": ""}).status_code == 400
        assert client.post("/hardware", files={"file": ("x.md", b"\xff\xfe")}).status_code == 400
        too_big = b"a" * (hardware.MAX_BYTES + 1)
        assert client.post("/hardware", files={"file": ("x.md", too_big)}).status_code == 400
        client.post("/hardware", data={"name": "dup"})
        assert client.post("/hardware", data={"name": "dup"}).status_code == 400

    def test_unknown_or_bad_names_are_404(self, client):
        assert client.get("/hardware/nope").status_code == 404
        assert client.get("/hardware/..%2Fconfig").status_code == 404
