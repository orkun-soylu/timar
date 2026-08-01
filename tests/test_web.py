"""Setup, login and access control.

Every test gets its own empty `/data` via `TIMAR_DATA`, because the app's whole notion of "has
this been set up" is a file on disk.
"""
import importlib

import pytest
from fastapi.testclient import TestClient

PASSWORD = "correct-horse-battery"


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


def complete_setup(client, username="op", password=PASSWORD):
    return client.post("/setup", data={"username": username, "password": password})


class TestFirstRun:
    def test_everything_redirects_to_setup_before_an_account_exists(self, client):
        """The gap between first boot and finishing setup must not serve the fleet inventory."""
        for path in ("/", "/login", "/fragments/fleet"):
            assert client.get(path).headers["location"] == "/setup"

    def test_health_answers_before_setup(self, client):
        # A container orchestrator has to be able to tell "starting" from "wedged".
        body = client.get("/health").json()
        assert body == {"status": "ok", "configured": False}

    def test_static_assets_are_served_before_setup(self, client):
        # The setup page needs its stylesheet and script, or it renders unusable.
        assert client.get("/static/htmx.min.js").status_code == 200

    def test_setup_creates_the_account_and_signs_in(self, client):
        response = complete_setup(client)
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert response.cookies.get("timar_session")

    def test_setup_is_closed_once_an_account_exists(self, client):
        """Otherwise the one route reachable without a session is a password reset for anyone."""
        complete_setup(client)
        assert client.get("/setup").headers["location"] == "/"

    def test_second_setup_post_cannot_overwrite_the_account(self, client):
        complete_setup(client)
        # Redirected away rather than accepted — belt to the create_account guard's braces.
        assert client.post("/setup", data={"username": "x", "password": PASSWORD}).status_code == 303
        from timar.web import auth
        assert auth.account()["username"] == "op"

    def test_short_password_is_rejected(self, client):
        response = client.post("/setup", data={"username": "op", "password": "short"})
        assert response.status_code == 400
        from timar.web import auth
        assert auth.account() is None


class TestSession:
    def test_dashboard_requires_a_session(self, client):
        complete_setup(client)
        client.cookies.clear()
        assert client.get("/").headers["location"] == "/login"

    def test_login_with_correct_password(self, client):
        complete_setup(client)
        client.cookies.clear()
        response = client.post("/login", data={"username": "op", "password": PASSWORD})
        assert response.status_code == 303 and response.cookies.get("timar_session")

    def test_wrong_password_gives_no_session(self, client):
        complete_setup(client)
        client.cookies.clear()
        response = client.post("/login", data={"username": "op", "password": "wrong-wrong-wrong"})
        assert response.status_code == 401
        assert not response.cookies.get("timar_session")

    def test_wrong_username_and_wrong_password_read_identically(self, client):
        """A different message tells an attacker which half of the guess to keep."""
        complete_setup(client)
        client.cookies.clear()
        bad_user = client.post("/login", data={"username": "nobody", "password": PASSWORD})
        bad_pass = client.post("/login", data={"username": "op", "password": "nope-nope-nope"})
        assert bad_user.status_code == bad_pass.status_code
        assert "incorrect username or password" in bad_user.text
        assert "incorrect username or password" in bad_pass.text

    def test_lockout_after_repeated_failures(self, client):
        complete_setup(client)
        client.cookies.clear()
        for _ in range(5):
            client.post("/login", data={"username": "op", "password": "wrong-wrong-wrong"})
        # The correct password is refused too — the lock is on the account, not the guess.
        response = client.post("/login", data={"username": "op", "password": PASSWORD})
        assert response.status_code == 401 and "too many attempts" in response.text

    def test_session_cookie_is_httponly_and_lax(self, client):
        header = complete_setup(client).headers["set-cookie"]
        assert "httponly" in header.lower()
        assert "samesite=lax" in header.lower()

    def test_logout_clears_the_cookie(self, client):
        complete_setup(client)
        response = client.post("/logout")
        assert response.status_code == 303
        assert 'timar_session=""' in response.headers["set-cookie"] or \
               "Max-Age=0" in response.headers["set-cookie"]

    def test_cookie_for_a_replaced_account_stops_working(self, client, tmp_path):
        """A restored volume can carry an old cookie into a fleet with a different operator."""
        complete_setup(client)
        cookie = client.cookies.get("timar_session")
        from timar import config
        from timar.web import auth
        config.path(config.AUTH).unlink()
        auth.create_account("someone-else", PASSWORD)
        assert auth.read_token(cookie) is None


class TestDashboard:
    def test_renders_with_no_servers_configured(self, client):
        complete_setup(client)
        response = client.get("/")
        assert response.status_code == 200
        assert "No servers configured yet." in response.text

    def test_lists_configured_servers(self, client, monkeypatch):
        complete_setup(client)
        from timar import config, status as fleet_status
        config.save({"servers": [
            {"name": "web-01", "host": "10.0.0.1"},
            {"name": "gpu-01", "host": "10.0.0.2", "wol_mac": "aa:bb:cc:dd:ee:ff"},
        ]})
        monkeypatch.setattr(fleet_status, "is_host_up", lambda host, **kw: host == "10.0.0.1")
        fleet_status.invalidate()

        # Asserted against the fragment, not the full page: the page also carries the state
        # words in its stylesheet and its legend, so a substring check there passes for the
        # wrong reason.
        rows = client.get("/fragments/fleet").text
        assert 'class="up"' in rows and 'class="asleep"' in rows
        # The on-demand machine is off, and that is reported as expected rather than as a fault.
        assert 'class="down"' not in rows

    def test_fleet_fragment_requires_a_session(self, client):
        complete_setup(client)
        client.cookies.clear()
        assert client.get("/fragments/fleet").headers["location"] == "/login"


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture_url(into: dict):
    def urlopen(url, timeout):
        into["url"] = url
        return _FakeResponse()
    return urlopen


class TestHealthcheck:
    """The container probe, which is a separate code path from the /health route."""

    def test_probes_the_configured_port(self, monkeypatch):
        """The port is configurable, so the probe must read it rather than assume 8080.

        A hardcoded port made the container report `starting` forever for anyone who changed
        TIMAR_PORT -- a change the compose file explicitly invites. It only reproduces by
        running the image, so it is pinned here where a build is not needed to catch it.
        """
        from timar.web import healthcheck

        seen: dict = {}
        monkeypatch.setenv("TIMAR_PORT", "9443")
        monkeypatch.setattr(healthcheck.urllib.request, "urlopen", _capture_url(seen))

        assert healthcheck.main() == 0
        assert "127.0.0.1:9443" in seen["url"]

    def test_defaults_to_8080(self, monkeypatch):
        from timar.web import healthcheck

        seen: dict = {}
        monkeypatch.delenv("TIMAR_PORT", raising=False)
        monkeypatch.setattr(healthcheck.urllib.request, "urlopen", _capture_url(seen))

        assert healthcheck.main() == 0
        assert "127.0.0.1:8080" in seen["url"]

    def test_unreachable_server_is_a_failure_not_a_traceback(self, monkeypatch):
        """Docker reads the exit code; an escaping exception is still a nonzero exit but the
        log then carries a traceback instead of a sentence naming the port."""
        from timar.web import healthcheck

        def refuse(url, timeout):
            raise OSError("connection refused")

        monkeypatch.setattr(healthcheck.urllib.request, "urlopen", refuse)
        assert healthcheck.main() == 1
