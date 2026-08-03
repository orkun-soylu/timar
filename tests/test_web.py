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


class TestPowerColumn:
    """The dashboard is where an operator already is when they notice a machine is asleep."""

    @pytest.fixture
    def fleet(self, client, monkeypatch):
        from timar import config, status as fleet_status
        complete_setup(client)
        config.save({"servers": [
            {"name": "web-01", "host": "10.0.0.1", "user": "op"},
            {"name": "gpu-01", "host": "10.0.0.2", "user": "op", "wol_mac": "aa:bb:cc:dd:ee:ff"},
            {"name": "gpu-02", "host": "10.0.0.3", "user": "op", "wol_mac": "aa:bb:cc:dd:ee:aa"},
        ]})
        # gpu-01 is on-demand and up; gpu-02 is on-demand and asleep; web-01 is always on.
        monkeypatch.setattr(fleet_status, "is_host_up",
                            lambda host, **kw: host in ("10.0.0.1", "10.0.0.2"))
        fleet_status.invalidate()
        return client

    def test_each_state_offers_the_action_that_fits_it(self, fleet):
        rows = fleet.get("/fragments/fleet").text
        assert "/servers/gpu-01/shutdown" in rows      # up, and wakeable again afterwards
        assert "/servers/gpu-02/wake" in rows          # asleep
        # An always-on machine has no wake path, so it is offered no way down.
        assert "/servers/web-01/shutdown" not in rows
        assert "n/a" in rows

    def test_the_shutdown_is_confirmed_first(self, fleet):
        assert "hx-confirm" in fleet.get("/fragments/fleet").text

    def test_requires_a_session(self, fleet):
        fleet.cookies.clear()
        for path in ("/servers/gpu-01/shutdown", "/servers/gpu-02/wake"):
            assert fleet.post(path).headers.get("location") == "/login"

    def test_unknown_server_is_404(self, fleet):
        assert fleet.post("/servers/nope/wake").status_code == 404

    def test_waking_reports_what_was_done(self, fleet, monkeypatch):
        monkeypatch.setattr("timar.web.app.power.wake",
                            lambda server, servers: f"magic packet sent to {server['name']}")
        body = fleet.post("/servers/gpu-02/wake").text
        assert "magic packet sent to gpu-02" in body and 'class="ok"' in body

    def test_a_failure_comes_back_as_a_readable_reason(self, fleet, monkeypatch):
        from timar import power

        def refuse(server, servers):
            raise power.PowerError("pve-01 is offline — wake it first, then try again")
        monkeypatch.setattr("timar.web.app.power.wake", refuse)
        body = fleet.post("/servers/gpu-02/wake").text
        assert "pve-01 is offline" in body and 'class="error"' in body

    def test_hypervisor_output_cannot_carry_markup_into_the_page(self, fleet, monkeypatch):
        from timar import power

        def refuse(server, servers):
            raise power.PowerError("<script>alert(1)</script>")
        monkeypatch.setattr("timar.web.app.power.shutdown", refuse)
        body = fleet.post("/servers/gpu-01/shutdown").text
        assert "<script>" not in body and "&lt;script&gt;" in body

    def test_the_cached_probe_is_dropped_so_the_next_poll_tells_the_truth(self, fleet,
                                                                         monkeypatch):
        from timar import status as fleet_status
        monkeypatch.setattr("timar.web.app.power.shutdown", lambda server, servers: "down it goes")
        fleet.get("/fragments/fleet")                      # populates the cache
        assert fleet_status._cache
        fleet.post("/servers/gpu-01/shutdown")
        assert not fleet_status._cache


class TestJobReport:
    """The findings behind a summary. Without this page an installation with no Telegram token
    swept its fleet and had nowhere to show what it found."""

    def test_shows_the_stored_report(self, client):
        complete_setup(client)
        from timar import state
        state.mark_finished(
            "log_sweep", ok=True, summary="1 with findings, 0 unreachable, 0 asleep",
            report="web-01:\n  stopped containers: cache, queue",
        )
        body = client.get("/jobs/log_sweep/report").text
        assert "stopped containers: cache, queue" in body
        assert "1 with findings" in body

    def test_the_link_appears_only_once_there_is_a_report(self, client):
        complete_setup(client)
        assert "/jobs/log_sweep/report" not in client.get("/fragments/jobs").text
        from timar import state
        state.mark_finished("log_sweep", ok=True, summary="all clear", report="All clear — 1 checked.")
        assert "/jobs/log_sweep/report" in client.get("/fragments/jobs").text

    def test_a_job_that_never_ran_says_so_rather_than_erroring(self, client):
        complete_setup(client)
        response = client.get("/jobs/update/report")
        assert response.status_code == 200
        assert "has not run yet" in response.text

    def test_a_failed_run_shows_its_error(self, client):
        complete_setup(client)
        from timar import state
        state.mark_finished("update", ok=False, error="SSHError: connection refused")
        assert "SSHError: connection refused" in client.get("/jobs/update/report").text

    def test_an_unknown_job_is_a_404_not_a_blank_page(self, client):
        complete_setup(client)
        assert client.get("/jobs/no-such-job/report").status_code == 404

    def test_requires_a_session(self, client):
        complete_setup(client)
        client.cookies.clear()
        assert client.get("/jobs/log_sweep/report").headers["location"] == "/login"

    def test_a_report_is_escaped_rather_than_rendered(self, client):
        """Findings carry remote log lines. A host that logs `<script>` must not run it here."""
        complete_setup(client)
        from timar import state
        state.mark_finished("log_sweep", ok=True, report="<script>alert(1)</script>")
        body = client.get("/jobs/log_sweep/report").text
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body


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


class TestSettingsTabs:
    """Two tabs, and the tab is a URL rather than a CSS state.

    Every form on this page saves with a POST and a redirect. A tab remembered only in the page
    would reset on the way back, dropping the operator on the server list to read a "Saved."
    about the notifier they were editing.
    """

    @pytest.fixture
    def settings(self, client):
        complete_setup(client)
        from timar import config
        config.save({"servers": [
            {"name": "web-01", "host": "10.0.0.1", "user": "deploy", "platform": "linux",
             "update_cmd": "make it so", "context": "logs resets nightly"},
            {"name": "hv-01", "host": "10.0.0.4", "user": "root", "platform": "proxmox",
             "wol_mac": "aa:bb:cc:dd:ee:01", "manages_vms": [{"vm_id": 100, "server_name": "vm-01"}]},
            {"name": "vm-01", "host": "10.0.0.40", "user": "deploy", "platform": "linux"},
        ]})
        return client

    def test_servers_is_the_default_tab_and_carries_no_global_settings(self, settings):
        page = settings.get("/settings").text
        assert "web-01" in page
        assert "Bot token" not in page and "Disk threshold" not in page

    def test_the_global_tab_carries_the_four_fleet_wide_sections(self, settings):
        page = settings.get("/settings?tab=global").text
        for heading in ("Log sweep", "Schedules", "Model", "Notifications"):
            assert f"<h2>{heading}</h2>" in page
        # And not the server list — the whole point of splitting them.
        assert "10.0.0.1" not in page

    def test_an_unknown_tab_opens_rather_than_404s(self, settings):
        """A stale bookmark should land somewhere useful."""
        response = settings.get("/settings?tab=nonsense")
        assert response.status_code == 200 and "web-01" in response.text

    def test_saving_a_global_form_comes_back_to_the_global_tab(self, settings):
        """Otherwise the confirmation appears on a tab the operator is not looking at."""
        for path, data in [
            ("/settings/log-check", {"journal_hours": "6", "disk_threshold": "85"}),
            ("/settings/telegram", {"token": "", "chat_id": ""}),
            ("/settings/llm", {"provider": "", "model": "", "base_url": "", "api_key": ""}),
            ("/settings/schedules", {}),
        ]:
            location = settings.post(path, data=data).headers["location"]
            assert location == "/settings?tab=global&notice=saved", path

    def test_a_rejected_global_form_stays_on_the_global_tab(self, settings):
        body = settings.post("/settings/log-check",
                             data={"journal_hours": "0", "disk_threshold": "85"}).text
        assert "Bot token" in body      # still the global tab, not bounced to the server list

    def test_saving_a_server_comes_back_to_the_server_tab(self, settings):
        location = settings.post("/settings/servers", data={
            "name": "new-01", "host": "10.0.0.9", "user": "deploy", "platform": "linux",
        }).headers["location"]
        assert location == "/settings?notice=saved"


class TestServerForm:
    """The add/edit form is one form in two modes, opened from the list rather than always on."""

    @pytest.fixture
    def settings(self, client):
        complete_setup(client)
        from timar import config
        config.save({"servers": [
            {"name": "web-01", "host": "10.0.0.1", "user": "deploy", "platform": "linux"},
            {"name": "hv-01", "host": "10.0.0.4", "user": "root", "platform": "proxmox",
             "wol_mac": "aa:bb:cc:dd:ee:01", "manages_vms": [{"vm_id": 100, "server_name": "vm-01"}]},
            {"name": "vm-01", "host": "10.0.0.40", "user": "deploy", "platform": "linux"},
        ]})
        return client

    def test_it_is_closed_until_asked_for(self, settings):
        page = settings.get("/settings").text
        assert 'id="server-form"' not in page
        # And the control that opens it is on the heading row, anchored to where it appears.
        assert "/settings?add=1#server-form" in page

    def test_the_plus_opens_it_empty(self, settings):
        page = settings.get("/settings?add=1").text
        assert 'id="server-form"' in page
        assert "Add a server" in page
        assert 'name="original_name"' not in page   # an add must not carry an edit's identity

    def test_edit_opens_it_filled(self, settings):
        page = settings.get("/settings?edit=hv-01").text
        assert 'value="hv-01"' in page and 'value="aa:bb:cc:dd:ee:01"' in page
        assert 'name="original_name" value="hv-01"' in page

    def test_a_guest_shows_the_link_that_lives_on_its_hypervisor(self, settings):
        """The relationship is stored on hv-01's entry, but it is vm-01's form that must show it."""
        import re
        page = settings.get("/settings?edit=vm-01").text
        assert re.search(r'<option value="hv-01"\s+selected', page)
        assert 'value="100"' in page

    def test_an_empty_fleet_opens_the_form_by_itself(self, client):
        """A first run whose only panel says "no servers" and offers nothing to press is a dead
        end."""
        complete_setup(client)
        page = client.get("/settings").text
        assert 'id="server-form"' in page and "Add a server" in page
        # Nothing to cancel back to, so no cancel link and no close control.
        assert ">cancel</a>" not in page

    def test_a_rejected_add_keeps_what_was_typed(self, settings):
        """It used to come back empty: the operator was told what was wrong with input that was
        no longer on the screen."""
        response = settings.post("/settings/servers", data={
            "name": "bad name", "host": "10.0.0.7", "user": "deploy", "platform": "linux",
            "update_cmd": "sudo apt-get upgrade -y",
        })
        assert response.status_code == 400
        page = response.text
        assert 'id="server-form"' in page                  # still open
        assert 'value="bad name"' in page                  # including the value that was refused
        assert 'value="10.0.0.7"' in page
        assert "sudo apt-get upgrade -y" in page
        assert "may contain only letters" in page

    def test_a_rejected_edit_keeps_the_edit_rather_than_the_stored_values(self, settings):
        """Re-rendering from storage would silently discard the change being complained about."""
        response = settings.post("/settings/servers", data={
            "original_name": "web-01", "name": "web-01", "host": "", "user": "deploy",
            "platform": "linux", "context": "half-finished note",
        })
        assert response.status_code == 400
        page = response.text
        assert 'name="original_name" value="web-01"' in page   # still an edit, not a new server
        assert "half-finished note" in page
        assert "10.0.0.1" not in page.split('id="server-form"')[1]  # not the stored address
        assert "Address is required." in page

    def test_a_rejected_rename_still_edits_the_original(self, settings):
        """Or saving again would add a second machine beside the one being renamed."""
        page = settings.post("/settings/servers", data={
            "original_name": "web-01", "name": "hv-01", "host": "10.0.0.1", "user": "deploy",
            "platform": "linux",
        }).text
        assert 'name="original_name" value="web-01"' in page
        assert "already exists" in page

    def test_a_stale_edit_link_does_not_open_an_edit_of_nothing(self, settings):
        """A bookmark to a server that has since been removed must not offer to save it back."""
        page = settings.get("/settings?edit=gone-01").text
        assert 'name="original_name"' not in page

    def test_a_server_cannot_be_its_own_relay_or_its_own_hypervisor(self, settings):
        """The lists exclude the entry being edited — both would be a cycle."""
        page = settings.get("/settings?edit=hv-01").text
        form = page.split('id="server-form"')[1]
        assert '<option value="hv-01"' not in form


class TestSettings:
    def test_every_settings_route_requires_a_session(self, client):
        """Declared on the router, so a new endpoint is protected for being one, not by memory."""
        complete_setup(client)
        client.cookies.clear()
        for method, path in [
            ("get", "/settings"),
            ("post", "/settings/servers"),
            ("post", "/settings/log-check"),
            ("post", "/settings/llm"),
            ("post", "/settings/llm/test"),
            ("post", "/settings/llm/models"),
            ("post", "/settings/telegram"),
            ("post", "/settings/telegram/test"),
            ("post", "/settings/servers/web-01/delete"),
        ]:
            response = getattr(client, method)(path)
            assert response.headers.get("location") == "/login", f"{method} {path} was not guarded"

    def test_add_a_server(self, client):
        complete_setup(client)
        from timar import config
        response = client.post("/settings/servers", data={
            "name": "web-01", "host": "10.0.0.1", "user": "deploy", "platform": "linux"})
        assert response.status_code == 303
        assert config.load()["servers"] == [
            {"name": "web-01", "host": "10.0.0.1", "user": "deploy", "platform": "linux"}]

    def test_invalid_server_is_rejected_and_nothing_is_written(self, client):
        complete_setup(client)
        from timar import config
        response = client.post("/settings/servers", data={"name": "bad name", "host": "", "user": "", "platform": "linux"})
        assert response.status_code == 400
        assert config.load().get("servers") in (None, [])

    def test_edit_replaces_in_place_and_keeps_order(self, client):
        complete_setup(client)
        from timar import config
        for name in ("a", "b", "c"):
            client.post("/settings/servers", data={
                "name": name, "host": f"10.0.0.{name}", "user": "deploy", "platform": "linux"})
        client.post("/settings/servers", data={
            "original_name": "b", "name": "b2", "host": "10.0.0.9",
            "user": "deploy", "platform": "openwrt"})
        names = [s["name"] for s in config.load()["servers"]]
        assert names == ["a", "b2", "c"]

    def test_delete_also_drops_the_hypervisor_relationship(self, client):
        """A guest left in manages_vms after its entry is gone is one nothing will ever start."""
        complete_setup(client)
        from timar import config
        config.save({"servers": [
            {"name": "hv", "host": "10.0.0.1", "user": "root", "platform": "proxmox",
             "manages_vms": [{"vm_id": 100, "server_name": "vm-01"}]},
            {"name": "vm-01", "host": "10.0.0.2", "user": "deploy", "platform": "linux"},
        ]})
        client.post("/settings/servers/vm-01/delete")
        remaining = config.load()["servers"]
        assert [s["name"] for s in remaining] == ["hv"]
        assert "manages_vms" not in remaining[0]

    def test_a_guest_can_be_linked_to_its_hypervisor_from_the_form(self, client):
        """Without this the relationship exists only in hand-edited YAML, so a VM added through
        the UI is permanently mislabelled as always-on with no way to correct it."""
        complete_setup(client)
        from timar import config
        client.post("/settings/servers", data={
            "name": "hv", "host": "10.0.0.1", "user": "root", "platform": "proxmox",
            "wol_mac": "aa:bb:cc:dd:ee:ff"})
        client.post("/settings/servers", data={
            "name": "vm-01", "host": "10.0.0.2", "user": "deploy", "platform": "linux",
            "hypervisor": "hv", "vm_id": "100"})
        hv = config.load()["servers"][0]
        assert hv["manages_vms"] == [{"vm_id": 100, "server_name": "vm-01"}]
        assert config.on_demand(config.load()["servers"])["vm-01"] == "hv"

    def test_the_settings_table_agrees_with_the_dashboard_about_a_guest(self, client):
        """The bug that prompted all this: the two pages described the same VM differently."""
        complete_setup(client)
        from timar import config
        config.save({"servers": [
            {"name": "hv", "host": "10.0.0.1", "user": "root", "platform": "proxmox",
             "wol_mac": "aa:bb:cc:dd:ee:ff",
             "manages_vms": [{"vm_id": 100, "server_name": "vm-01"}]},
            {"name": "vm-01", "host": "10.0.0.2", "user": "deploy", "platform": "linux"},
        ]})
        page = client.get("/settings").text
        row = page.split("<td>vm-01</td>", 1)[1].split("</tr>", 1)[0]
        assert "on-demand" in row and "via hv" in row
        assert "always on" not in row

    def test_the_update_timeout_field_shows_the_default_and_round_trips_an_override(self, client):
        """The default has to be visible in the form, or the only way to learn it is the source."""
        complete_setup(client)
        from timar import config
        from timar.updater import DEFAULT_UPDATE_TIMEOUT
        config.save({"servers": [
            {"name": "gpu-01", "host": "10.0.0.5", "user": "ops", "platform": "linux",
             "update_timeout": 3600},
        ]})

        page = client.get("/settings?edit=gpu-01").text
        field = page.split('name="update_timeout"', 1)[1].split(">", 1)[0]
        assert 'value="3600"' in field
        assert f'placeholder="{DEFAULT_UPDATE_TIMEOUT}"' in field

    def test_moving_a_guest_leaves_only_one_hypervisor_owning_it(self, client):
        complete_setup(client)
        from timar import config
        config.save({"servers": [
            {"name": "hv-a", "host": "10.0.0.1", "user": "root", "platform": "proxmox",
             "manages_vms": [{"vm_id": 100, "server_name": "vm-01"}]},
            {"name": "hv-b", "host": "10.0.0.2", "user": "root", "platform": "proxmox"},
            {"name": "vm-01", "host": "10.0.0.3", "user": "deploy", "platform": "linux"},
        ]})
        client.post("/settings/servers", data={
            "original_name": "vm-01", "name": "vm-01", "host": "10.0.0.3", "user": "deploy",
            "platform": "linux", "hypervisor": "hv-b", "vm_id": "200"})
        by_name = {s["name"]: s for s in config.load()["servers"]}
        assert "manages_vms" not in by_name["hv-a"]
        assert by_name["hv-b"]["manages_vms"] == [{"vm_id": 200, "server_name": "vm-01"}]

    def test_clearing_the_hypervisor_detaches_the_guest(self, client):
        complete_setup(client)
        from timar import config
        config.save({"servers": [
            {"name": "hv", "host": "10.0.0.1", "user": "root", "platform": "proxmox",
             "manages_vms": [{"vm_id": 100, "server_name": "vm-01"}]},
            {"name": "vm-01", "host": "10.0.0.2", "user": "deploy", "platform": "linux"},
        ]})
        client.post("/settings/servers", data={
            "original_name": "vm-01", "name": "vm-01", "host": "10.0.0.2", "user": "deploy",
            "platform": "linux", "hypervisor": "", "vm_id": ""})
        assert "manages_vms" not in config.load()["servers"][0]

    def test_renaming_a_guest_carries_the_hypervisor_link(self, client):
        """A rename that updates only the entry leaves a guest nothing will ever start."""
        complete_setup(client)
        from timar import config
        config.save({"servers": [
            {"name": "hv", "host": "10.0.0.1", "user": "root", "platform": "proxmox",
             "wol_mac": "aa:bb:cc:dd:ee:ff",
             "manages_vms": [{"vm_id": 100, "server_name": "vm-01"}]},
            {"name": "vm-01", "host": "10.0.0.2", "user": "deploy", "platform": "linux"},
        ]})
        client.post("/settings/servers", data={
            "original_name": "vm-01", "name": "kali", "host": "10.0.0.2", "user": "deploy",
            "platform": "linux", "hypervisor": "hv", "vm_id": "100"})
        servers = config.load()["servers"]
        assert servers[0]["manages_vms"] == [{"vm_id": 100, "server_name": "kali"}]
        assert config.on_demand(servers)["kali"] == "hv"

    def test_renaming_a_relay_carries_the_reference(self, client):
        complete_setup(client)
        from timar import config
        config.save({"servers": [
            {"name": "jump", "host": "10.0.0.1", "user": "deploy", "platform": "linux"},
            {"name": "gpu", "host": "10.0.0.2", "user": "deploy", "platform": "linux",
             "wol_mac": "aa:bb:cc:dd:ee:ff", "wol_relay": "jump"},
        ]})
        client.post("/settings/servers", data={
            "original_name": "jump", "name": "jump-01", "host": "10.0.0.1", "user": "deploy",
            "platform": "linux"})
        assert config.load()["servers"][1]["wol_relay"] == "jump-01"

    def test_stored_secrets_are_never_sent_to_the_browser(self, client):
        """The page says a key is stored; it never says what it is."""
        complete_setup(client)
        from timar import config
        cfg = config.load()
        cfg["llm"] = {"provider": "anthropic", "model": "m", "api_key": "SECRET-LLM-KEY"}
        cfg["telegram"] = {"token": "SECRET-BOT-TOKEN", "chat_id": "123"}
        config.save(cfg)

        page = client.get("/settings?tab=global").text
        assert "SECRET-LLM-KEY" not in page
        assert "SECRET-BOT-TOKEN" not in page
        assert "stored — leave blank to keep it" in page
        assert "123" in page  # the chat id is not a secret and must round-trip

    def test_saving_the_form_blank_does_not_wipe_the_key(self, client):
        complete_setup(client)
        from timar import config
        cfg = config.load()
        cfg["llm"] = {"provider": "anthropic", "model": "m", "api_key": "keep-me"}
        config.save(cfg)
        client.post("/settings/llm", data={"provider": "anthropic", "model": "m2", "api_key": ""})
        stored = config.load()["llm"]
        assert stored["api_key"] == "keep-me" and stored["model"] == "m2"

    def test_test_button_reports_failure_without_leaking_html(self, client, monkeypatch):
        complete_setup(client)
        from timar import config, llm as llm_module
        cfg = config.load()
        cfg["llm"] = {"provider": "ollama", "model": "m", "base_url": "http://x"}
        config.save(cfg)

        def boom(*a, **kw):
            raise llm_module.LLMError("<script>alert(1)</script> refused")
        monkeypatch.setattr("timar.web.settings.llm_module.complete", boom)

        body = client.post("/settings/llm/test").text
        assert "<script>" not in body and "&lt;script&gt;" in body

    def test_test_button_says_so_when_nothing_is_configured(self, client):
        complete_setup(client)
        assert "Save a model connection first" in client.post("/settings/llm/test").text


class TestEnrolmentRoutes:
    def test_requires_a_session(self, client):
        complete_setup(client)
        from timar import config
        config.save({"servers": [{"name": "a", "host": "h", "user": "u", "platform": "linux"}]})
        client.cookies.clear()
        for method, path in [("get", "/settings/servers/a/enroll"),
                             ("post", "/settings/servers/a/enroll"),
                             ("post", "/settings/servers/a/verify")]:
            assert getattr(client, method)(path).headers.get("location") == "/login"

    def test_unknown_server_is_404(self, client):
        complete_setup(client)
        assert client.get("/settings/servers/nope/enroll").status_code == 404

    def test_form_shows_the_fingerprint_but_never_a_private_key(self, client):
        complete_setup(client)
        from timar import config
        config.save({"servers": [{"name": "a", "host": "h", "user": "u", "platform": "linux"}]})
        page = client.get("/settings/servers/a/enroll").text
        assert "SHA256:" in page
        assert "ssh-ed25519 " in page
        assert "PRIVATE KEY" not in page

    def test_sudo_option_is_hidden_where_it_cannot_work(self, client):
        complete_setup(client)
        from timar import config
        config.save({"servers": [
            {"name": "router", "host": "h", "user": "root", "platform": "openwrt"},
            {"name": "box", "host": "h", "user": "root", "platform": "linux"},
            {"name": "web", "host": "h", "user": "deploy", "platform": "linux"},
        ]})
        assert 'name="grant_sudo"' not in client.get("/settings/servers/router/enroll").text
        assert 'name="grant_sudo"' not in client.get("/settings/servers/box/enroll").text
        assert 'name="grant_sudo"' in client.get("/settings/servers/web/enroll").text

    def test_the_password_is_never_echoed_back(self, client, monkeypatch):
        """Re-rendering the form with the field refilled would put it in browser history and
        in every proxy in between."""
        complete_setup(client)
        from timar import config, enroll
        config.save({"servers": [{"name": "a", "host": "h", "user": "u", "platform": "linux"}]})

        def refuse(*a, **kw):
            raise enroll.EnrollError("the password was not accepted for that user")
        monkeypatch.setattr("timar.web.settings.enroll_module.enroll", refuse)

        response = client.post("/settings/servers/a/enroll",
                               data={"password": "s3cret-passphrase", "grant_sudo": "on"})
        assert response.status_code == 400
        assert "s3cret-passphrase" not in response.text
        assert "was not accepted" in response.text

    def test_missing_password_is_rejected_before_connecting(self, client, monkeypatch):
        complete_setup(client)
        from timar import config
        config.save({"servers": [{"name": "a", "host": "h", "user": "u", "platform": "linux"}]})

        called = []
        monkeypatch.setattr("timar.web.settings.enroll_module.enroll",
                            lambda *a, **kw: called.append(1))
        response = client.post("/settings/servers/a/enroll", data={"password": ""})
        assert response.status_code == 400 and not called

    def test_success_reports_the_verification_not_just_the_install(self, client, monkeypatch):
        """The password connection succeeding says nothing about whether the key is accepted."""
        complete_setup(client)
        from timar import config, enroll
        config.save({"servers": [{"name": "a", "host": "h", "user": "u", "platform": "linux"}]})
        monkeypatch.setattr("timar.web.settings.enroll_module.enroll",
                            lambda *a, **kw: enroll.Result(key_installed=True))
        monkeypatch.setattr("timar.web.settings.enroll_module.verify",
                            lambda server: "connected with the key as u; passwordless sudo works")
        page = client.post("/settings/servers/a/enroll", data={"password": "pw"}).text
        assert "key installed" in page and "passwordless sudo works" in page


class TestModelListing:
    """Typing a model name from memory is how `claude-opus-5` becomes `claude-opus5`."""

    def test_lists_the_models_the_provider_offers(self, client, monkeypatch):
        complete_setup(client)
        from timar import config, llm as llm_module
        config.save({"llm": {"provider": "ollama", "base_url": "http://10.0.0.1:11434"}})
        monkeypatch.setattr(llm_module, "list_models", lambda _cfg: ["glm-5.2:cloud", "kimi-k2.6"])

        body = client.post("/settings/llm/models").text
        assert '<datalist id="model-options">' in body
        assert '<option value="glm-5.2:cloud">' in body
        assert "2 models" in body

    def test_a_provider_error_is_reported_not_raised(self, client, monkeypatch):
        complete_setup(client)
        from timar import config, llm as llm_module
        config.save({"llm": {"provider": "ollama", "base_url": "http://10.0.0.1:11434"}})

        def fail(_cfg):
            raise llm_module.LLMError("could not reach ollama")

        monkeypatch.setattr(llm_module, "list_models", fail)
        response = client.post("/settings/llm/models")
        assert response.status_code == 200
        assert "could not reach ollama" in response.text
        assert "datalist" not in response.text

    def test_no_provider_saved_says_so_rather_than_erroring(self, client):
        complete_setup(client)
        assert "Save a provider first" in client.post("/settings/llm/models").text

    def test_a_provider_with_no_models_is_reported(self, client, monkeypatch):
        complete_setup(client)
        from timar import config, llm as llm_module
        config.save({"llm": {"provider": "ollama", "base_url": "http://10.0.0.1:11434"}})
        monkeypatch.setattr(llm_module, "list_models", lambda _cfg: [])
        assert "listed no models" in client.post("/settings/llm/models").text

    def test_model_names_are_escaped_into_the_datalist(self, client, monkeypatch):
        """Model names come from a remote provider and land in an HTML attribute."""
        complete_setup(client)
        from timar import config, llm as llm_module
        config.save({"llm": {"provider": "ollama", "base_url": "http://10.0.0.1:11434"}})
        monkeypatch.setattr(llm_module, "list_models", lambda _cfg: ['"><script>alert(1)</script>'])
        body = client.post("/settings/llm/models").text
        assert "<script>" not in body
        assert "&lt;script&gt;" in body

    def test_the_model_field_is_wired_to_the_datalist(self, client):
        complete_setup(client)
        assert 'list="model-options"' in client.get("/settings?tab=global").text


class TestReportArchive:
    """The series, not the snapshot.

    `state.json` answers "what did the last sweep find". It cannot answer "when did this
    start" — a disk creeping past 90%, an update failing every Friday. Only a history can.
    """

    @staticmethod
    def archive(job, **fields):
        from timar import reports
        return reports.archive(job, title=fields.pop("title", job),
                               ok=fields.pop("ok", True), **fields)

    def test_lists_archived_runs_newest_first(self, client):
        complete_setup(client)
        self.archive("log_sweep", summary="older run")
        self.archive("log_sweep", summary="newer run")
        body = client.get("/reports").text
        assert body.index("newer run") < body.index("older run")

    def test_the_dropdown_offers_every_job_with_its_count(self, client):
        complete_setup(client)
        self.archive("update", title="Update run", summary="3 updated")
        body = client.get("/reports").text
        assert "Update run (1)" in body
        # Offered even with nothing archived: an empty list is the answer to "why have I seen
        # no sweep report", which the filter should be able to ask.
        assert "Log sweep (0)" in body

    def test_filtering_narrows_the_list(self, client):
        complete_setup(client)
        self.archive("log_sweep", title="Log sweep", summary="1 with findings")
        self.archive("update", title="Update run", summary="3 updated")
        body = client.get("/reports?job=update").text
        assert "3 updated" in body and "1 with findings" not in body

    def test_an_unknown_job_shows_an_empty_list_rather_than_an_error(self, client):
        """The value can come from a stale bookmark naming a job that no longer exists."""
        complete_setup(client)
        response = client.get("/reports?job=retired")
        assert response.status_code == 200 and "No reports archived" in response.text

    def test_an_archived_report_is_shown_in_full(self, client):
        complete_setup(client)
        report_id = self.archive("log_sweep", title="Log sweep",
                                 report="web-01:\n  disk 91% on /")
        assert "disk 91% on /" in client.get(f"/reports/{report_id}").text

    def test_an_archived_report_links_back_to_its_own_filter(self, client):
        """Comparing four update runs must not mean re-picking the filter between each one."""
        complete_setup(client)
        report_id = self.archive("update", title="Update run", report="ok")
        assert 'href="/reports?job=update"' in client.get(f"/reports/{report_id}").text

    def test_an_archived_report_is_escaped_rather_than_rendered(self, client):
        """Findings carry remote log lines. A host that logs `<script>` must not run it here."""
        complete_setup(client)
        report_id = self.archive("log_sweep", report="<script>alert(1)</script>")
        body = client.get(f"/reports/{report_id}").text
        assert "<script>alert(1)</script>" not in body and "&lt;script&gt;" in body

    def test_an_id_that_climbs_out_of_the_archive_is_a_404(self, client):
        """`auth.json` holds the password hash and lives one directory up."""
        complete_setup(client)
        assert client.get("/reports/..%2Fauth.json").status_code == 404
        assert client.get("/reports/20260101-000000.000000-update").status_code == 404

    def test_the_archive_requires_a_session(self, client):
        complete_setup(client)
        report_id = self.archive("update", report="fleet inventory")
        client.cookies.clear()
        assert client.get("/reports").headers["location"] == "/login"
        assert client.get(f"/reports/{report_id}").headers["location"] == "/login"

    def test_the_dashboard_links_to_the_archive(self, client):
        complete_setup(client)
        assert 'href="/reports"' in client.get("/").text

    def test_the_history_link_appears_only_once_something_is_archived(self, client):
        complete_setup(client)
        assert "/reports?job=update" not in client.get("/fragments/jobs").text
        self.archive("update", title="Update run", summary="3 updated")
        assert "/reports?job=update" in client.get("/fragments/jobs").text
