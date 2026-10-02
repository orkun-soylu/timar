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

    def test_health_answers_a_head_probe(self, client):
        """Uptime monitors probe with HEAD first; a 405 there costs every check a retry."""
        for configured in (False, True):
            if configured:
                complete_setup(client)
            response = client.head("/health")
            assert response.status_code == 200 and response.content == b""

    def test_every_page_carries_the_same_tab_icon_as_timar_tools(self, client):
        """Inline, so it needs no route — and matches the project's page, byte for byte."""
        import re
        from pathlib import Path
        site = (Path(__file__).parent.parent / "docs" / "index.html").read_text()
        mark = re.search(r'<link rel="icon" href="([^"]+)"', site).group(1)
        assert f'<link rel="icon" href="{mark}">' in client.get("/setup").text
        complete_setup(client)
        for path in ("/", "/settings", "/reports"):
            assert f'<link rel="icon" href="{mark}">' in client.get(path).text, path

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
        # A first run whose only panel says "no servers" and offers nothing to press is a dead end.
        assert 'href="/settings/servers/new"' in response.text

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


class TestActionsColumn:
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
        # Always on and up: it can be shut down, with a confirmation that says it stays off.
        assert "/servers/web-01/shutdown" in rows
        assert "/servers/web-01/wake" not in rows
        web = rows.split("/servers/web-01/shutdown", 1)[1].split("</button>", 1)[0]
        assert "switches it on by hand" in web
        gpu = rows.split("/servers/gpu-01/shutdown", 1)[1].split("</button>", 1)[0]
        assert "until something wakes it" in gpu

    def test_a_guest_of_an_always_on_host_gets_both_buttons(self, client, monkeypatch):
        """Always on, but `qm start` brings it back: a wake when it is down, and a shutdown
        whose confirmation does not claim it will stay off."""
        from timar import config, status as fleet_status
        complete_setup(client)
        config.save({"servers": [
            {"name": "hv", "host": "10.0.0.1", "user": "root", "platform": "proxmox",
             "manages_vms": [{"vm_id": 100, "server_name": "vm-a"},
                             {"vm_id": 101, "server_name": "vm-b"}]},
            {"name": "vm-a", "host": "10.0.0.2", "user": "op"},
            {"name": "vm-b", "host": "10.0.0.3", "user": "op"},
        ]})
        monkeypatch.setattr(fleet_status, "is_host_up", lambda host, **kw: host != "10.0.0.3")
        fleet_status.invalidate()
        rows = client.get("/fragments/fleet").text
        assert "/servers/vm-b/wake" in rows
        vm_a = rows.split("/servers/vm-a/shutdown", 1)[1].split("</button>", 1)[0]
        assert "until something wakes it" in vm_a
        # The hypervisor itself has no wake path: shutting it down is final.
        hv = rows.split("/servers/hv/shutdown", 1)[1].split("</button>", 1)[0]
        assert "switches it on by hand" in hv

    def test_a_machine_switched_on_by_hand_can_be_shut_down_but_not_woken(
            self, client, monkeypatch):
        from timar import config, status as fleet_status
        complete_setup(client)
        config.save({"servers": [
            {"name": "printer-a", "host": "10.0.0.4", "user": "op", "on_demand": True},
            {"name": "printer-b", "host": "10.0.0.5", "user": "op", "on_demand": True},
        ]})
        monkeypatch.setattr(fleet_status, "is_host_up", lambda host, **kw: host == "10.0.0.4")
        fleet_status.invalidate()
        rows = client.get("/fragments/fleet").text
        assert "/servers/printer-a/shutdown" in rows      # up
        assert "/servers/printer-b/shutdown" not in rows  # off
        for name in ("printer-a", "printer-b"):
            assert f"/servers/{name}/wake" not in rows
        assert rows.split("printer-b", 1)[0].rsplit("<tr ", 1)[1].startswith('class="asleep"')

    def test_every_row_offers_edit_and_nothing_else_of_its_own(self, fleet):
        """Enrolment and removal live in the edit form."""
        rows = fleet.get("/fragments/fleet").text
        assert "/enroll" not in rows and "/delete" not in rows
        for name in ("web-01", "gpu-01", "gpu-02"):
            assert f'href="/settings/servers/{name}/edit"' in rows
        # The add button is on the heading row, not repeated per server.
        assert rows.count('href="/settings/servers/new"') == 1

    def test_remove_is_in_the_edit_form_and_asks_first(self, fleet):
        dialog = fleet.get("/settings/servers/web-01/edit", headers={"HX-Request": "true"}).text
        button = dialog.split('hx-post="/settings/servers/web-01/delete"', 1)[1].split(">", 1)[0]
        assert "Remove web-01 from timar?" in button and "hx-confirm" in button
        page = fleet.get("/settings/servers/web-01/edit").text
        button = page.split('formaction="/settings/servers/web-01/delete"', 1)[1].split(">", 1)[0]
        assert "confirm(" in button and "Remove web-01 from timar?" in button
        # Not offered for a server that does not exist yet.
        assert "/delete" not in fleet.get("/settings/servers/new").text

    def test_removing_goes_back_to_the_dashboard(self, fleet):
        response = fleet.post("/settings/servers/web-01/delete")
        assert response.headers["location"] == "/"

    def test_the_state_is_a_light_before_the_name_with_its_word_on_hover(self, fleet):
        rows = fleet.get("/fragments/fleet").text
        assert ">State<" not in rows                     # no column of its own any more
        cell = rows.split('<tr class="asleep">', 1)[1].split("</td>", 1)[0]
        assert 'title="asleep (on-demand) · Linux"' in cell and 'class="os"' in cell
        assert cell.rstrip().endswith("gpu-02")
        assert 'title="up · Linux"' in rows

    def test_a_web_interface_makes_the_name_a_link_while_up(self, client, monkeypatch):
        from timar import config, status as fleet_status
        complete_setup(client)
        config.save({"servers": [
            {"name": "hv-up", "host": "10.0.0.1", "user": "root", "web_url": "https://10.0.0.1:8006"},
            {"name": "hv-off", "host": "10.0.0.2", "user": "root", "web_url": "https://10.0.0.2:8006",
             "wol_mac": "aa:bb:cc:dd:ee:ff"},
            {"name": "plain", "host": "10.0.0.3", "user": "op"},
        ]})
        monkeypatch.setattr(fleet_status, "is_host_up", lambda host, **kw: host != "10.0.0.2")
        fleet_status.invalidate()
        rows = client.get("/fragments/fleet").text
        cell = rows.split('<tr class="up">', 1)[1].split("</td>", 1)[0]
        assert '<a href="https://10.0.0.1:8006" target="_blank" rel="noopener noreferrer"' in cell
        assert cell.rstrip().endswith(">hv-up</a>")
        # Asleep: the panel would not answer, so no link — the name is plain text.
        assert "10.0.0.2:8006" not in rows
        # The sort helper still finds every name, linked or not.
        assert self.order(rows) == ["hv-off", "hv-up", "plain"]

    def test_a_program_link_opens_no_tab_and_a_hand_written_script_is_dropped(self, client, monkeypatch):
        """`claude://open` hands off to a program; a new tab would be left behind empty. And the
        file is edited by hand too, so a value the form would refuse must not reach the page."""
        from timar import config, status as fleet_status
        complete_setup(client)
        config.save({"link_schemes": ["claude"], "servers": [
            {"name": "term", "host": "10.0.0.1", "user": "op", "web_url": "claude://open"},
            {"name": "evil", "host": "10.0.0.2", "user": "op", "web_url": "javascript:alert(1)"},
            {"name": "gone", "host": "10.0.0.3", "user": "op", "web_url": "pi://open"},
        ]})
        monkeypatch.setattr(fleet_status, "is_host_up", lambda host, **kw: True)
        fleet_status.invalidate()
        rows = client.get("/fragments/fleet").text
        assert '<a href="claude://open" rel="noopener noreferrer"' in rows
        assert "javascript" not in rows
        # Not in link_schemes (any more): no link, just the name.
        assert "pi://open" not in rows

    def test_the_light_is_the_machine_s_logo_in_its_state_s_colour(self, client, monkeypatch):
        """Read off a connection, by address; a Proxmox host is Proxmox though it reads Debian;
        a machine never connected to falls back to its platform."""
        import json
        from timar import config, osinfo, status as fleet_status
        complete_setup(client)
        config.save({"servers": [
            {"name": "hv", "host": "10.0.0.5", "user": "root", "platform": "proxmox"},
            {"name": "gpu", "host": "10.0.0.41", "user": "op", "platform": "linux"},
            {"name": "rt", "host": "10.0.0.1", "user": "root", "platform": "openwrt"},
        ]})
        path = config.path(osinfo.OS_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"10.0.0.5": "debian",
                                    "10.0.0.41": {"logo": "ubuntu", "version": "Ubuntu 26.04"}}))
        monkeypatch.setattr(fleet_status, "is_host_up", lambda host, **kw: host != "10.0.0.41")
        fleet_status.invalidate()
        rows = client.get("/fragments/fleet").text
        assert 'title="up · Proxmox"' in rows and 'logos.svg#proxmox' in rows
        assert 'title="down · Ubuntu"' in rows and 'logos.svg#ubuntu' in rows
        assert 'title="up · OpenWrt"' in rows and 'logos.svg#openwrt' in rows
        # System: what was read, else the platform.
        assert ">Ubuntu 26.04<" in rows and ">openwrt<" in rows
        # The colour comes from the row's state class, which the logo inherits.
        down = rows.split('<tr class="down">', 1)[1].split("</td>", 1)[0]
        assert 'class="os"' in down and "ubuntu" in down

    def test_the_summary_counts_every_state_and_filters_by_one(self, client, monkeypatch):
        from timar import config, status as fleet_status
        complete_setup(client)
        config.save({"servers": [
            {"name": "a", "host": "10.0.0.1", "user": "op"},
            {"name": "b", "host": "10.0.0.2", "user": "op"},
            {"name": "c", "host": "10.0.0.3", "user": "op", "wol_mac": "aa:bb:cc:dd:ee:03"},
        ]})
        monkeypatch.setattr(fleet_status, "is_host_up", lambda host, **kw: host != "10.0.0.3")
        fleet_status.invalidate()
        rows = client.get("/fragments/fleet").text
        assert ">2 up</a>" in rows and ">1 asleep</a>" in rows and ">0 down</a>" in rows
        assert "chip-zero" in rows and ">all " not in rows
        picked = client.get("/fragments/fleet?state=asleep&sort=name&dir=asc").text
        # Still counted over the whole fleet; only the table is filtered.
        assert ">2 up</a>" in picked and 'aria-current="true">1 asleep</a>' in picked
        assert self.order(picked) == ["c"] and ">all 3</a>" in picked
        # A sort link keeps the filter, and so does the poll.
        assert "sort=address&amp;dir=asc&amp;state=asleep" in picked
        page = client.get("/?state=asleep").text
        assert "/fragments/fleet?sort=name&amp;dir=asc&amp;state=asleep" in page
        # An empty filter is not an empty fleet.
        assert "No machine is in this state" in client.get("/fragments/fleet?state=down").text
        # Anything else is no filter at all.
        assert len(self.order(client.get("/fragments/fleet?state=bogus").text)) == 3

    @staticmethod
    def order(html):
        import re
        return re.findall(r'</use></svg> (?:<a [^>]*>)?([\w-]+)(?:</a>)?</td>', html)

    def test_sorts_by_address_numerically(self, fleet):
        from timar import config
        cfg = config.load()
        cfg["servers"][0]["host"] = "10.0.0.10"          # web-01 now sorts after 10.0.0.9-ish
        config.save(cfg)
        rows = fleet.get("/fragments/fleet?sort=address").text
        assert self.order(rows) == ["gpu-01", "gpu-02", "web-01"]
        rows = fleet.get("/fragments/fleet?sort=address&dir=desc").text
        assert self.order(rows) == ["web-01", "gpu-02", "gpu-01"]

    def test_sorts_by_platform_then_name(self, fleet):
        from timar import config
        cfg = config.load()
        cfg["servers"][1]["platform"] = "openwrt"         # gpu-01
        config.save(cfg)
        rows = fleet.get("/fragments/fleet?sort=platform").text
        assert self.order(rows) == ["gpu-02", "web-01", "gpu-01"]

    def test_an_unknown_sort_falls_back_to_the_name(self, fleet):
        rows = fleet.get("/fragments/fleet?sort=nonsense&dir=sideways").text
        assert self.order(rows) == ["gpu-01", "gpu-02", "web-01"]

    def test_the_poll_keeps_the_chosen_order(self, fleet):
        """A sort the next ten-second refresh undoes is not a sort."""
        page = fleet.get("/?sort=address&dir=desc").text
        assert 'hx-get="/fragments/fleet?sort=address&amp;dir=desc"' in page

    def test_the_active_heading_flips_the_direction(self, fleet):
        rows = fleet.get("/fragments/fleet?sort=address").text
        assert 'href="/?sort=address&amp;dir=desc"' in rows
        assert 'aria-sort="ascending"' in rows
        assert 'href="/?sort=name&amp;dir=asc"' in rows

    def test_rows_are_sorted_by_name_not_config_order(self, fleet):
        rows = fleet.get("/fragments/fleet").text
        # Configured web-01, gpu-01, gpu-02 — shown alphabetically.
        assert rows.index("gpu-01") < rows.index("gpu-02") < rows.index("web-01")

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

    def test_the_row_carries_run_only_not_report_or_history(self, client):
        """The full list is on the same page, under the panel."""
        complete_setup(client)
        from timar import state
        state.mark_finished("log_sweep", ok=True, summary="all clear", report="All clear — 1 checked.")
        rows = client.get("/fragments/jobs").text
        assert "/jobs/log_sweep/report" not in rows and "/reports?job=" not in rows
        assert "/jobs/log_sweep/run" in rows and "/jobs/log_sweep/stop" not in rows

    def test_a_running_job_offers_stop_with_a_warning_instead_of_run(self, client, monkeypatch):
        from timar.web import app as web
        complete_setup(client)
        monkeypatch.setattr(web.scheduler, "_running", {"update"})
        rows = client.get("/fragments/jobs").text
        stop = rows.split('hx-post="/jobs/update/stop"', 1)[1].split(">", 1)[0]
        assert "hx-confirm" in stop and "still shut down" in stop
        assert "/jobs/update/run" not in rows and "/jobs/log_sweep/run" in rows

    def test_stop_asks_the_running_job_and_the_row_says_stopping(self, client, monkeypatch):
        from timar import cancel
        from timar.web import app as web
        complete_setup(client)
        monkeypatch.setattr(web.scheduler, "_running", {"update"})
        try:
            rows = client.post("/jobs/update/stop").text
            assert cancel.requested("update") and "stopping…" in rows
            # A job that is not running is not marked.
            client.post("/jobs/log_sweep/stop")
            assert not cancel.requested("log_sweep")
            assert client.post("/jobs/nope/stop").status_code == 404
        finally:
            cancel.clear("update")

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


class TestSettingsPage:
    """Only the fleet-wide settings. Servers are managed from the dashboard."""

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

    def test_it_carries_model_and_notifications_only(self, settings):
        """The log sweep and the schedules moved to the reports page's + dialog."""
        page = settings.get("/settings").text
        for heading, help_id in (("Model", "help-model"), ("Notifications", "help-notifications")):
            assert f'<h2 class="with-action">{heading}' in page
            assert f'popovertarget="{help_id}"' in page and f'id="{help_id}" popover' in page
        assert 'name="journal_hours"' not in page and "_enabled" not in page
        # The explanations live in the popovers, not on the page.
        outside = page.split('id="help-model"', 1)[0]
        assert 'class="hint"' not in outside and "drops any comments" not in outside
        assert "10.0.0.1" not in page and "web-01" not in page

    def test_the_old_global_tab_url_still_opens(self, settings):
        """A stale bookmark should land somewhere useful."""
        response = settings.get("/settings?tab=global")
        assert response.status_code == 200 and "Bot token" in response.text

    @pytest.mark.parametrize("query, location", [
        ("edit=web-01", "/settings/servers/web-01/edit"),
        ("enroll=web-01", "/settings/servers/web-01/edit"),
        ("add=1", "/settings/servers/new"),
    ])
    def test_old_server_links_lead_to_where_the_panel_lives_now(self, settings, query, location):
        response = settings.get(f"/settings?{query}")
        assert response.status_code == 303 and response.headers["location"] == location

    @pytest.mark.parametrize("query", [
        "edit=gone-01", "enroll=gone-01", "edit=//evil.example", "enroll=/../../x%0d%0aSet-Cookie:a=b",
    ])
    def test_an_old_link_to_no_such_server_opens_the_page_and_redirects_nowhere(self, settings,
                                                                                query):
        """Only a stored name is ever put in a Location header — never what the query carried."""
        response = settings.get(f"/settings?{query}")
        assert response.status_code == 200 and "location" not in response.headers

    def test_saving_a_global_form_comes_back_with_a_notice(self, settings):
        for path, data in [
            ("/settings/telegram", {"token": "", "chat_id": ""}),
            ("/settings/llm", {"provider": "", "model": "", "base_url": "", "api_key": ""}),
        ]:
            location = settings.post(path, data=data).headers["location"]
            assert location == "/settings?notice=saved", path

    def test_a_rejected_global_form_stays_on_the_page(self, settings):
        response = settings.post("/settings/telegram", data={"token": "t", "chat_id": ""})
        assert response.status_code == 400 and "Bot token" in response.text

    def test_saving_a_server_goes_back_to_the_dashboard(self, settings):
        location = settings.post("/settings/servers", data={
            "name": "new-01", "host": "10.0.0.9", "user": "deploy", "platform": "linux",
        }).headers["location"]
        assert location == "/"


class TestJobEdit:
    """Each job's settings, opened by the pencil on its row in Scheduled work."""

    def test_the_heading_has_only_its_question_mark(self, client):
        complete_setup(client)
        page = client.get("/reports").text
        heading = page.split("Scheduled work", 1)[1].split("</h2>", 1)[0]
        assert 'popovertarget="help-jobs"' in heading and "/settings/jobs" not in heading
        assert page.index('id="dialog-body"') > page.index("<body")

    def test_each_row_has_a_pencil_beside_run(self, client):
        complete_setup(client)
        rows = client.get("/fragments/jobs").text
        for name in ("log_sweep", "update"):
            assert f'hx-get="/settings/jobs/{name}/edit"' in rows and f"/jobs/{name}/run" in rows

    def test_the_sweep_dialog_holds_its_schedule_and_its_settings_filled(self, client):
        from timar import config
        complete_setup(client)
        cfg = config.load()
        cfg["schedules"] = {"log_sweep": {"enabled": True, "kind": "daily", "at": "09:30"}}
        cfg["log_check"] = {"journal_hours": 25, "disk_threshold": 90}
        config.save(cfg)
        body = client.get("/settings/jobs/log_sweep/edit", headers={"HX-Request": "true"}).text
        assert 'name="log_sweep_enabled" checked' in body and 'value="09:30"' in body
        assert 'value="25"' in body and 'value="90"' in body
        assert "update_enabled" not in body and 'id="help-job-update"' not in body
        assert 'class="hint"' not in body and "(for daily and weekly)" not in body

    def test_the_update_dialog_has_no_sweep_settings(self, client):
        complete_setup(client)
        body = client.get("/settings/jobs/update/edit", headers={"HX-Request": "true"}).text
        assert 'name="update_enabled"' in body and "journal_hours" not in body

    def test_saving_one_job_leaves_the_other_alone(self, client):
        from timar import config
        complete_setup(client)
        cfg = config.load()
        cfg["schedules"] = {"log_sweep": {"enabled": True, "kind": "daily", "at": "09:30"}}
        config.save(cfg)
        response = client.post("/settings/jobs/update", data={
            "update_enabled": "on", "update_kind": "weekly", "update_at": "07:00",
            "update_day": "friday"}, headers={"HX-Request": "true"})
        assert response.headers["HX-Redirect"] == "/reports"
        schedules = config.load()["schedules"]
        assert schedules["update"]["day"] == "friday"
        assert schedules["log_sweep"] == {"enabled": True, "kind": "daily", "at": "09:30"}

    def test_the_sweep_saves_its_settings_too(self, client):
        from timar import config
        complete_setup(client)
        client.post("/settings/jobs/log_sweep", data={
            "journal_hours": "12", "disk_threshold": "90", "log_sweep_kind": "daily",
            "log_sweep_at": "09:30"})
        assert config.load()["log_check"] == {"journal_hours": 12, "disk_threshold": 90}

    def test_a_mistake_writes_nothing_and_keeps_what_was_typed(self, client):
        from timar import config
        complete_setup(client)
        before = config.load()
        response = client.post("/settings/jobs/log_sweep", data={
            "journal_hours": "0", "disk_threshold": "85", "log_sweep_enabled": "on",
            "log_sweep_kind": "weekly", "log_sweep_at": "07:00", "log_sweep_day": "friday"},
            headers={"HX-Request": "true"})
        assert response.status_code == 200 and "between 1 and 168" in response.text
        assert 'value="friday" selected' in response.text
        assert config.load().get("schedules") == before.get("schedules")

    def test_an_unknown_job_is_404(self, client):
        complete_setup(client)
        assert client.get("/settings/jobs/nope/edit").status_code == 404
        assert client.post("/settings/jobs/nope", data={}).status_code == 404


class TestServerForm:
    """The add/edit form is one form in two modes, opened in the dashboard's dialog."""

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

    HX = {"HX-Request": "true"}

    def test_the_plus_opens_it_empty(self, settings):
        page = settings.get("/settings/servers/new").text
        assert 'id="server-form"' in page
        assert "Add a server" in page
        assert 'name="original_name"' not in page   # an add must not carry an edit's identity

    def test_edit_opens_it_filled(self, settings):
        page = settings.get("/settings/servers/hv-01/edit").text
        assert 'value="hv-01"' in page and 'value="aa:bb:cc:dd:ee:01"' in page
        assert 'name="original_name" value="hv-01"' in page

    def test_editing_an_unknown_server_is_404(self, settings):
        assert settings.get("/settings/servers/gone-01/edit").status_code == 404

    def test_a_guest_shows_the_link_that_lives_on_its_hypervisor(self, settings):
        """The relationship is stored on hv-01's entry, but it is vm-01's form that must show it."""
        import re
        page = settings.get("/settings/servers/vm-01/edit").text
        assert re.search(r'<option value="hv-01"\s+selected', page)
        assert 'value="100"' in page

    def test_the_dialog_has_a_close_control_top_right_and_a_page_does_not(self, settings):
        bare = settings.get("/settings/servers/new", headers=self.HX).text
        heading = bare.split("<h2", 1)[1].split("</h2>", 1)[0]
        assert "data-close-dialog" in heading
        page = settings.get("/settings/servers/new").text
        assert "data-close-dialog" not in page.split("<h2", 1)[1].split("</h2>", 1)[0]

    def test_htmx_gets_the_bare_panel_and_a_page_gets_a_page(self, settings):
        """The dialog wants the panel alone; without scripting the same link has to be a page."""
        bare = settings.get("/settings/servers/new", headers=self.HX).text
        assert "<html" not in bare and 'id="server-form"' in bare
        assert 'hx-post="/settings/servers"' in bare and "data-close-dialog" in bare
        page = settings.get("/settings/servers/new").text
        assert "<html" in page and "hx-post" not in page.split('id="server-form"')[1]

    def test_a_rejected_save_in_the_dialog_comes_back_as_the_form(self, settings):
        """htmx does not swap an error response: a 400 would leave the dialog silent."""
        response = settings.post("/settings/servers", headers=self.HX, data={
            "name": "bad name", "host": "10.0.0.7", "user": "deploy", "platform": "linux"})
        assert response.status_code == 200
        assert 'value="bad name"' in response.text and "may contain only letters" in response.text

    def test_a_save_in_the_dialog_reloads_the_dashboard(self, settings):
        response = settings.post("/settings/servers", headers=self.HX, data={
            "name": "new-01", "host": "10.0.0.9", "user": "deploy", "platform": "linux"})
        assert response.headers.get("HX-Redirect") == "/"

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

    def test_a_server_cannot_be_its_own_relay_or_its_own_hypervisor(self, settings):
        """The lists exclude the entry being edited — both would be a cycle."""
        page = settings.get("/settings/servers/hv-01/edit").text
        form = page.split('id="server-form"')[1]
        assert '<option value="hv-01"' not in form


class TestSettings:
    def test_every_settings_route_requires_a_session(self, client):
        """Declared on the router, so a new endpoint is protected for being one, not by memory."""
        complete_setup(client)
        client.cookies.clear()
        for method, path in [
            ("get", "/settings"),
            ("get", "/settings/servers/new"),
            ("get", "/settings/servers/web-01/edit"),
            ("post", "/settings/servers"),
            ("get", "/settings/jobs/update/edit"),
            ("post", "/settings/jobs/update"),
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

    def test_a_web_interface_is_saved_and_shown_again_in_the_edit_form(self, client):
        complete_setup(client)
        from timar import config
        client.post("/settings/servers", data={
            "name": "hv", "host": "10.0.0.1", "user": "root", "platform": "proxmox",
            "web_url": "10.0.0.1:8006"})
        assert config.load()["servers"][0]["web_url"] == "https://10.0.0.1:8006"
        form = client.get("/settings/servers/hv/edit").text
        assert 'value="https://10.0.0.1:8006"' in form
        # Cleared in the form, it is gone — not carried over as if it were hand-written.
        client.post("/settings/servers", data={
            "original_name": "hv", "name": "hv", "host": "10.0.0.1", "user": "root",
            "platform": "proxmox", "web_url": ""})
        assert "web_url" not in config.load()["servers"][0]

    def test_a_program_link_is_saved_only_once_the_file_allows_its_scheme(self, client):
        complete_setup(client)
        from timar import config
        form = {"name": "term", "host": "10.0.0.1", "user": "op", "platform": "linux",
                "web_url": "claude://open"}
        page = client.post("/settings/servers", data=form)
        assert page.status_code == 400 and "link_schemes" in page.text
        assert config.load().get("servers", []) == []
        cfg = config.load()
        cfg["link_schemes"] = ["claude"]
        config.save(cfg)
        client.post("/settings/servers", data=form)
        assert config.load()["servers"][0]["web_url"] == "claude://open"

    def test_an_edit_keeps_hand_written_wake_settings_and_shows_them(self, client):
        complete_setup(client)
        from timar import config
        config.save({"servers": [
            {"name": "relay-01", "host": "10.0.0.1", "user": "op", "platform": "linux"},
            {"name": "gpu-01", "host": "10.1.0.2", "user": "op", "platform": "linux",
             "wol_mac": "aa:bb:cc:dd:ee:ff", "wol_broadcast": "10.1.0.255", "wol_relay": "relay-01"},
        ]})
        form = client.get("/settings/servers/gpu-01/edit").text
        assert 'name="wol_relay"' not in form and 'name="wol_broadcast"' not in form
        assert "Woken through relay-01." in form and "Broadcast address 10.1.0.255." in form
        client.post("/settings/servers", data={
            "original_name": "gpu-01", "name": "gpu-01", "host": "10.1.0.2", "user": "op",
            "platform": "linux", "wol_mac": "aa:bb:cc:dd:ee:ff", "context": "edited"})
        saved = config.load()["servers"][1]
        assert saved["context"] == "edited"
        assert (saved["wol_relay"], saved["wol_broadcast"]) == ("relay-01", "10.1.0.255")

    def test_the_platform_defaults_open_from_the_update_command(self, client):
        complete_setup(client)
        import html
        from timar.platforms import PLATFORMS
        form = client.get("/settings/servers/new").text
        assert 'popovertarget="platform-defaults"' in form and 'id="platform-defaults" popover' in form
        for platform in PLATFORMS.values():
            assert platform.label in form
            if platform.default_update_cmd:
                assert html.escape(platform.default_update_cmd, quote=False) in form
        assert "None — swept, never updated." in form          # OpenWrt

    def test_edit_keeps_the_fields_the_form_does_not_show(self, client):
        """The form is not the whole entry. `job_logs`, `watch_logs` and `ssh_key` are written by
        hand, have no input on this page, and an edit that rebuilt the entry from the form alone
        deleted them — silently, and in the one direction nothing reports: the sweep simply stops
        watching a backup, and a job that is no longer watched writes no error anywhere.
        """
        complete_setup(client)
        from timar import config
        config.save({"servers": [{
            "name": "web-01", "host": "10.0.0.1", "user": "deploy", "platform": "linux",
            "ssh_key": "~/.ssh/legacy_ed25519",
            "job_logs": [{"path": "/var/log/backup.log",
                          "started_marker": "Backup started",
                          "completed_marker": "Backup completed"}],
            "watch_logs": ["/var/log/myapp.log"],
        }]})

        client.post("/settings/servers", data={
            "original_name": "web-01", "name": "web-01", "host": "10.0.0.2",
            "user": "deploy", "platform": "linux"})

        entry = config.load()["servers"][0]
        assert entry["host"] == "10.0.0.2"          # the edit still applied
        assert entry["ssh_key"] == "~/.ssh/legacy_ed25519"
        assert entry["job_logs"][0]["path"] == "/var/log/backup.log"
        assert entry["watch_logs"] == ["/var/log/myapp.log"]

    def test_editing_a_hypervisor_does_not_orphan_its_guests(self, client):
        """Same rebuild, worse blow. `manages_vms` names the guests this host is responsible for,
        and dropping it leaves a VM nothing will ever start — and one that `config.on_demand` then
        calls always-on, so every night it correctly spends powered off is reported as an outage.
        """
        complete_setup(client)
        from timar import config
        config.save({"servers": [
            {"name": "hv-01", "host": "10.0.0.1", "user": "root", "platform": "proxmox",
             "wol_mac": "aa:bb:cc:dd:ee:01",
             "manages_vms": [{"vm_id": 100, "server_name": "vm-01"}]},
            {"name": "vm-01", "host": "10.0.0.2", "user": "deploy", "platform": "linux"},
        ]})

        client.post("/settings/servers", data={
            "original_name": "hv-01", "name": "hv-01", "host": "10.0.0.9",
            "user": "root", "platform": "proxmox", "wol_mac": "aa:bb:cc:dd:ee:01"})

        servers = config.load()["servers"]
        assert servers[0]["manages_vms"] == [{"vm_id": 100, "server_name": "vm-01"}]
        assert config.on_demand(servers)["vm-01"] == "hv-01"

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

    def test_a_guest_of_an_always_on_host_can_be_marked_on_demand_from_the_form(self, client):
        """Inheritance calls every guest of an always-on host always-on, which is right until the
        guest is a VM started only when it is needed. The flag has to be settable where the link
        is, or it exists only in hand-edited YAML."""
        complete_setup(client)
        from timar import config
        config.save({"servers": [
            {"name": "hv", "host": "10.0.0.1", "user": "root", "platform": "proxmox"},
        ]})
        client.post("/settings/servers", data={
            "name": "vm-01", "host": "10.0.0.2", "user": "deploy", "platform": "linux",
            "hypervisor": "hv", "vm_id": "100", "on_demand": "on"})
        servers = config.load()["servers"]
        assert servers[0]["manages_vms"] == [
            {"vm_id": 100, "server_name": "vm-01", "on_demand": True}]
        assert config.on_demand(servers) == {"vm-01": "hv"}

        field = client.get("/settings/servers/vm-01/edit").text.split('name="on_demand"', 1)[1]
        assert field.split(">", 1)[0].strip().startswith("checked")

    def test_a_standalone_machine_can_be_marked_switched_on_by_hand(self, client):
        complete_setup(client)
        from timar import config
        config.save({"servers": []})
        client.post("/settings/servers", data={
            "name": "printer", "host": "10.0.0.50", "user": "pi", "platform": "linux",
            "on_demand": "on"})
        [server] = config.load()["servers"]
        assert server["on_demand"] is True
        assert config.on_demand([server]) == {"printer": config.MANUAL}

        field = client.get("/settings/servers/printer/edit").text.split('name="on_demand"', 1)[1]
        assert field.split(">", 1)[0].strip().startswith("checked")

        # Unticking it clears it — the form owns the field.
        client.post("/settings/servers", data={
            "original_name": "printer", "name": "printer", "host": "10.0.0.50", "user": "pi",
            "platform": "linux"})
        assert "on_demand" not in config.load()["servers"][0]

    def test_the_flag_is_not_stored_next_to_a_wake_address(self, client):
        """With a MAC the machine is on-demand already; a second switch would do nothing."""
        complete_setup(client)
        from timar import config
        config.save({"servers": []})
        client.post("/settings/servers", data={
            "name": "gpu-01", "host": "10.0.0.2", "user": "op", "platform": "linux",
            "wol_mac": "aa:bb:cc:dd:ee:ff", "on_demand": "on"})
        assert "on_demand" not in config.load()["servers"][0]

    def test_unticking_the_flag_makes_the_guest_always_on_again(self, client):
        complete_setup(client)
        from timar import config
        config.save({"servers": [
            {"name": "hv", "host": "10.0.0.1", "user": "root", "platform": "proxmox",
             "manages_vms": [{"vm_id": 100, "server_name": "vm-01", "on_demand": True}]},
            {"name": "vm-01", "host": "10.0.0.2", "user": "deploy", "platform": "linux"},
        ]})
        client.post("/settings/servers", data={
            "original_name": "vm-01", "name": "vm-01", "host": "10.0.0.2", "user": "deploy",
            "platform": "linux", "hypervisor": "hv", "vm_id": "100"})
        servers = config.load()["servers"]
        assert servers[0]["manages_vms"] == [{"vm_id": 100, "server_name": "vm-01"}]
        assert config.on_demand(servers) == {}

    def test_editing_the_hypervisor_keeps_its_guests_flag(self, client):
        """The hypervisor's form has no input for its guests; saving it must not drop the flag
        any more than it may drop the guest."""
        complete_setup(client)
        from timar import config
        config.save({"servers": [
            {"name": "hv", "host": "10.0.0.1", "user": "root", "platform": "proxmox",
             "manages_vms": [{"vm_id": 100, "server_name": "vm-01", "on_demand": True}]},
            {"name": "vm-01", "host": "10.0.0.2", "user": "deploy", "platform": "linux"},
        ]})
        client.post("/settings/servers", data={
            "original_name": "hv", "name": "hv", "host": "10.0.0.9",
            "user": "root", "platform": "proxmox"})
        assert config.on_demand(config.load()["servers"]) == {"vm-01": "hv"}

    def test_the_update_timeout_field_shows_the_default_and_round_trips_an_override(self, client):
        """The default has to be visible in the form, or the only way to learn it is the source."""
        complete_setup(client)
        from timar import config
        from timar.updater import DEFAULT_UPDATE_TIMEOUT
        config.save({"servers": [
            {"name": "gpu-01", "host": "10.0.0.5", "user": "ops", "platform": "linux",
             "update_timeout": 3600},
        ]})

        page = client.get("/settings/servers/gpu-01/edit").text
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

        page = client.get("/settings").text
        assert "SECRET-LLM-KEY" not in page
        assert "SECRET-BOT-TOKEN" not in page
        assert 'placeholder="stored"' in page
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


class TestEnrolInTheForm:
    """Enrolment is the server form's SSH section: Save saves, Enrol saves and then enrols."""

    SERVER = {"name": "a", "host": "h", "user": "u", "platform": "linux"}

    @pytest.fixture
    def form(self, client):
        complete_setup(client)
        from timar import config
        config.save({"servers": [dict(self.SERVER)]})
        return client

    def post(self, client, **fields):
        data = {**self.SERVER, "original_name": "a", **fields}
        return client.post("/settings/servers", data=data, headers={"HX-Request": "true"})

    def test_requires_a_session(self, form):
        form.cookies.clear()
        assert form.get("/settings/servers/a/enroll").headers.get("location") == "/login"
        assert form.post("/settings/servers", data={}).headers.get("location") == "/login"

    def test_an_old_enrol_link_opens_the_ssh_section(self, form):
        response = form.get("/settings/servers/a/enroll")
        assert response.status_code == 303
        assert response.headers["location"] == "/settings/servers/a/edit#ssh-access"
        assert form.get("/settings/servers/nope/enroll").status_code == 404

    def test_three_sections_each_with_its_help(self, form):
        page = form.get("/settings/servers/a/edit").text
        for title, help_id in [("Server info", "help-server"), ("Update", "help-update"),
                               ("SSH access", "help-ssh")]:
            assert f"<h3>{title}</h3>" in page
            assert f'popovertarget="{help_id}"' in page and f'id="{help_id}" popover' in page
        # The fields carry no explanations of their own; those are in the popovers.
        assert 'class="hint"' not in page.split('id="help-server"', 1)[0]

    def test_enter_saves_rather_than_enrols(self, form):
        """Implicit submission uses the first submit button in the form."""
        page = form.get("/settings/servers/a/edit").text
        form = page.split('action="/settings/servers"', 1)[1]
        first = form.split('type="submit"', 1)[1].split(">", 1)[0]
        assert 'value="save"' in first

    def test_the_help_shows_the_fingerprint_but_never_a_private_key(self, form):
        page = form.get("/settings/servers/a/edit").text
        assert "SHA256:" in page and "ssh-ed25519 " in page
        assert "PRIVATE KEY" not in page

    def test_save_ignores_the_password(self, form, monkeypatch):
        called = []
        monkeypatch.setattr("timar.web.settings.enroll_module.enroll", lambda *a, **kw: called.append(1))
        response = self.post(form, action="save", password="s3cret-passphrase")
        assert response.headers.get("HX-Redirect") == "/" and not called

    def test_enrol_saves_first_then_enrols_with_the_password(self, form, monkeypatch):
        from timar import config, enroll
        seen = {}

        def fake_enroll(server, password, *, grant_sudo):
            seen.update(server=server, password=password, sudo=grant_sudo)
            return enroll.Result(key_installed=True)
        monkeypatch.setattr("timar.web.settings.enroll_module.enroll", fake_enroll)
        monkeypatch.setattr("timar.web.settings.enroll_module.verify",
                            lambda server: "connected with the key as u; passwordless sudo works")
        page = self.post(form, action="enrol", password="pw", grant_sudo="on",
                         context="saved before enrolling").text
        assert config.load()["servers"][0]["context"] == "saved before enrolling"
        assert seen["password"] == "pw" and seen["sudo"] is True
        assert seen["server"]["context"] == "saved before enrolling"
        # The outcome is the proof with the key alone, not just the install.
        assert "key installed" in page and "passwordless sudo works" in page

    def test_enrol_without_a_password_only_checks_the_key(self, form, monkeypatch):
        called = []
        monkeypatch.setattr("timar.web.settings.enroll_module.enroll", lambda *a, **kw: called.append(1))
        monkeypatch.setattr("timar.web.settings.enroll_module.verify",
                            lambda server: "connected with the key as u; no passwordless sudo")
        page = self.post(form, action="enrol", password="").text
        assert not called and "Key checked: connected with the key as u" in page

    def test_the_password_is_never_echoed_back(self, form, monkeypatch):
        """Refilled, it would sit in browser history and in every proxy in between."""
        from timar import enroll

        def refuse(*a, **kw):
            raise enroll.EnrollError("the password was not accepted for that user")
        monkeypatch.setattr("timar.web.settings.enroll_module.enroll", refuse)
        page = self.post(form, action="enrol", password="s3cret-passphrase").text
        assert "s3cret-passphrase" not in page and "was not accepted" in page
        # Not on a rejected form either.
        page = self.post(form, action="enrol", password="s3cret-passphrase", name="bad name").text
        assert "s3cret-passphrase" not in page and "Name may contain only" in page


class TestModelListing:
    """Typing a model name from memory is how `claude-opus-5` becomes `claude-opus5`."""

    def test_lists_the_models_the_provider_offers(self, client, monkeypatch):
        complete_setup(client)
        from timar import config, llm as llm_module
        config.save({"llm": {"provider": "ollama", "base_url": "http://10.0.0.1:11434"}})
        monkeypatch.setattr(llm_module, "list_models", lambda _cfg: ["glm-5.2:cloud", "kimi-k2.6"])

        body = client.post("/settings/llm/models").text
        assert '<select class="model-pick"' in body and "this.form.model.value" in body
        assert '<option value="glm-5.2:cloud">glm-5.2:cloud</option>' in body
        assert "2 models" in body
        # Never submitted with the form: the field is what is saved.
        assert '<select name=' not in body

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
        assert "<select" not in response.text

    def test_no_provider_saved_says_so_rather_than_erroring(self, client):
        complete_setup(client)
        assert "Save a provider first" in client.post("/settings/llm/models").text

    def test_a_provider_with_no_models_is_reported(self, client, monkeypatch):
        complete_setup(client)
        from timar import config, llm as llm_module
        config.save({"llm": {"provider": "ollama", "base_url": "http://10.0.0.1:11434"}})
        monkeypatch.setattr(llm_module, "list_models", lambda _cfg: [])
        assert "listed no models" in client.post("/settings/llm/models").text

    def test_model_names_are_escaped_into_the_picker(self, client, monkeypatch):
        """Model names come from a remote provider and land in an HTML attribute."""
        complete_setup(client)
        from timar import config, llm as llm_module
        config.save({"llm": {"provider": "ollama", "base_url": "http://10.0.0.1:11434"}})
        monkeypatch.setattr(llm_module, "list_models", lambda _cfg: ['"><script>alert(1)</script>'])
        body = client.post("/settings/llm/models").text
        assert "<script>" not in body
        assert "&lt;script&gt;" in body

    def test_the_model_field_has_no_datalist(self, client):
        """A datalist is filtered by the field's value: with a model saved it hid most of the list."""
        complete_setup(client)
        assert 'list="model-options"' not in client.get("/settings").text

    def test_the_help_names_the_default_the_code_uses(self, client):
        complete_setup(client)
        from timar import llm as llm_module
        default = llm_module.DEFAULTS["anthropic"]["model"]
        assert f"its default model is <code>{default}</code>" in client.get("/settings").text


class TestTopNav:
    """One header on every signed-in page; only the highlight moves."""

    @staticmethod
    def header(body):
        return body[body.index('<header class="topnav">'):body.index("</header>")]

    @pytest.mark.parametrize("path, current", [
        ("/", "servers"), ("/reports", "reports"), ("/settings", "settings"),
        ("/settings/servers/new", "servers"), ("/jobs/update/report", "reports"),
        ("/containers", "containers"), ("/settings/containers/new", "containers"),
    ])
    def test_every_page_carries_it_with_its_own_place_highlighted(self, client, path, current):
        complete_setup(client)
        header = self.header(client.get(path).text)
        assert f'class="current" aria-current="page">{current}<' in header
        assert header.count('aria-current="page"') == 1
        import re
        order = re.findall(r'<a href="([^"]+)"', header)
        assert order == ["/", "/", "/containers", "/reports", "/settings"]   # brand, then the four
        assert header.index('action="/lang"') < header.index('action="/logout"')

    def test_the_name_is_lowercase_and_nothing_else_names_the_operator(self, client):
        complete_setup(client)
        header = self.header(client.get("/").text)
        assert '<a href="/" class="brand">timar</a>' in header
        assert "operator" not in header and ">op<" not in header
        assert "🌐" not in header
        assert "dashboard" not in client.get("/settings").text

    def test_action_results_go_to_the_header_toast(self, client, monkeypatch):
        """Beside the name, in the sticky header — not under the table, out of sight."""
        from timar import config, status as fleet_status
        complete_setup(client)
        monkeypatch.setattr(fleet_status, "is_host_up", lambda host, **kw: True)   # gets a power button
        fleet_status.invalidate()
        config.save({"servers": [{"name": "web-01", "host": "10.0.0.1", "user": "op"}],
                     "containers": [{"name": "app", "server": "web-01", "path": "/srv/app"}]})
        page = client.get("/").text
        header = page[page.index('<header class="topnav">'):page.index("</header>")]
        assert header.index('class="brand"') < header.index('id="toast"') < header.index("<nav")
        assert "power-result" not in page and "container-result" not in client.get("/containers").text
        assert 'hx-target="#toast"' in client.get("/fragments/fleet").text
        assert "setTimeout" in page and "3000" in page

    def test_scheduled_work_lives_on_the_reports_page(self, client):
        complete_setup(client)
        assert "jobs-panel" not in client.get("/").text
        reports_page = client.get("/reports").text
        assert reports_page.index('id="jobs-panel"') < reports_page.index('id="report-list"')


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

    def test_the_filter_offers_every_job_with_its_count(self, client):
        complete_setup(client)
        self.archive("update", title="Update run", summary="3 updated")
        body = client.get("/reports").text
        current = lambda html: html.split('aria-current="true"', 1)[1].split(">", 1)[1].split("</a>", 1)[0]
        assert ">Update run 1</a>" in body and current(body) == "All reports 1"
        # Offered even with nothing archived: an empty list is the answer to "why have I seen
        # no sweep report", which the filter should be able to ask.
        assert ">Log sweep 0</a>" in body
        # A plain link that HTMX upgrades, swapping the chips with the list so the pick moves.
        assert 'href="/reports?job=update"' in body and 'hx-select="#report-view"' in body
        picked = client.get("/reports?job=update").text
        assert current(picked) == "Update run 1"

    def test_filtering_narrows_the_list(self, client):
        complete_setup(client)
        self.archive("log_sweep", title="Log sweep", summary="1 with findings")
        self.archive("update", title="Update run", summary="3 updated")
        body = client.get("/reports?job=update").text
        assert "3 updated" in body and "1 with findings" not in body

    def test_a_finished_time_is_marked_for_the_relative_form_and_reads_without_it(self, client):
        """The script turns it into "2 hours ago"; without scripting it is the minute it ended."""
        import re
        complete_setup(client)
        self.archive("update", title="Update run", summary="3 updated")
        body = client.get("/reports").text
        stamp = re.search(r'<time datetime="(\d{4}-\d\d-\d\dT\d\d:\d\d[^"]*)" data-rel>([^<]+)</time>', body)
        assert stamp and stamp.group(2) == stamp.group(1)[:16].replace("T", " ")

    def test_an_unknown_job_shows_an_empty_list_rather_than_an_error(self, client):
        """The value can come from a stale bookmark naming a job that no longer exists."""
        complete_setup(client)
        response = client.get("/reports?job=retired")
        assert response.status_code == 200 and "No reports archived" in response.text

    def test_each_run_opens_from_an_icon_button_under_an_actions_heading(self, client):
        complete_setup(client)
        entry = self.archive("update", title="Update run", summary="3 updated")
        body = client.get("/reports?job=update").text
        assert ">Actions</th>" in body
        link = body.split(f'href="/reports/{entry}"', 1)[1].split("</a>", 1)[0]
        assert 'class="icon-btn"' in link and 'title="report"' in link and "<svg" in link

    def test_an_archived_report_is_shown_in_full(self, client):
        complete_setup(client)
        report_id = self.archive("log_sweep", title="Log sweep",
                                 report="web-01:\n  disk 91% on /")
        assert "disk 91% on /" in client.get(f"/reports/{report_id}").text

    def test_an_archived_report_sits_under_reports_in_the_menu(self, client):
        complete_setup(client)
        report_id = self.archive("update", title="Update run", report="ok")
        body = client.get(f"/reports/{report_id}").text
        assert 'class="current" aria-current="page">reports<' in body
        assert "Update run · Archived run" in body

    def test_a_report_opens_in_the_dialog_and_still_as_a_page(self, client):
        complete_setup(client)
        report_id = self.archive("update", title="Update run", summary="3 updated", report="all fine")
        listing = client.get("/reports").text
        row = listing.split(f'href="/reports/{report_id}"', 1)[1].split(">", 1)[0]
        assert f'hx-get="/reports/{report_id}"' in row and 'hx-target="#dialog-body"' in row
        bare = client.get(f"/reports/{report_id}", headers={"HX-Request": "true"}).text
        assert "<html" not in bare and 'id="report-panel"' in bare and "data-close-dialog" in bare
        assert "all fine" in bare
        page = client.get(f"/reports/{report_id}").text
        assert "<html" in page and "all fine" in page and "data-close-dialog" not in page

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
        assert 'href="/reports"' in client.get("/").text   # through the menu

    def test_the_page_keeps_its_notes_behind_the_question_marks(self, client):
        complete_setup(client)
        page = client.get("/reports").text
        assert 'popovertarget="help-jobs"' in page and 'popovertarget="help-reports"' in page
        outside = page.split('id="help-jobs"', 1)[0]
        for note in ("the schedule has stopped even though everything else looks healthy",
                     "whether or not it was also sent to Telegram"):
            assert note not in outside and note in page
