"""Compose projects: reading their state, validating entries, and the page and its actions."""
import json

import pytest

from timar import containers, validate
from tests.test_web import client, complete_setup  # noqa: F401 — the fixture


def ps_line(working_dir, name, state="running", status="Up 2 days", health="none", cid=None,
            image="img:1"):
    return json.dumps({
        "ID": cid or ("0" * 63 + name[-1]), "Names": name, "Image": image, "State": state,
        "Status": status, "HealthStatus": health,
        "Labels": f"com.docker.compose.project={name},"
                  f"com.docker.compose.project.working_dir={working_dir},"
                  "traefik.http.routers.x.middlewares=a@file,b@file",
    })


class TestParse:
    def test_groups_by_working_directory_and_skips_containers_compose_did_not_start(self):
        out = "\n".join([
            ps_line("/srv/immich", "immich-server"),
            ps_line("/srv/immich/", "immich-db"),
            ps_line("/srv/brain", "brain"),
            json.dumps({"ID": "x", "Names": "loose", "State": "running", "Labels": ""}),
            "not json",
        ])
        projects = containers.parse_ps(out)
        assert sorted(projects) == ["/srv/brain", "/srv/immich"]
        assert [c.name for c in projects["/srv/immich"]] == ["immich-server", "immich-db"]

    def test_reads_the_exit_code(self):
        c = containers.parse_ps(ps_line("/p", "a", "exited", "Exited (137) 3 hours ago"))["/p"][0]
        assert (c.state, c.exit_code) == ("exited", 137)


def make(state="running", status="Up", health="none", name="c1"):
    return containers.parse_ps(ps_line("/p", name, state, status, health))["/p"][0]


class TestProjectState:
    def test_running(self):
        assert containers.project_state([make()], False) == ("up", "running")

    def test_a_finished_one_shot_does_not_degrade_it(self):
        done = make("exited", "Exited (0) 1 hour ago", name="init")
        assert containers.project_state([make(), done], False)[0] == "up"

    def test_a_container_that_failed_does(self):
        failed = make("exited", "Exited (1) 1 hour ago", name="db")
        state, detail = containers.project_state([make(), failed], False)
        assert state == "warn" and "db" in detail

    def test_an_unhealthy_container_does(self):
        assert containers.project_state([make(health="unhealthy")], False)[0] == "warn"

    @pytest.mark.parametrize("on_demand, expected", [(False, "down"), (True, "asleep")])
    def test_stopped_is_down_unless_on_demand(self, on_demand, expected):
        stopped = make("exited", "Exited (0) 1 day ago")
        assert containers.project_state([stopped], on_demand)[0] == expected
        assert containers.project_state([], on_demand)[0] == expected


class TestValidate:
    SERVERS = [{"name": "docker-01", "platform": "linux"}, {"name": "router", "platform": "openwrt"}]

    def entry(self, **form):
        return validate.container({"name": "immich", "server": "docker-01", "path": "/srv/immich/",
                                   **form}, set(), self.SERVERS)

    def test_minimal_entry_drops_the_trailing_slash(self):
        assert self.entry() == {"name": "immich", "server": "docker-01", "path": "/srv/immich"}

    @pytest.mark.parametrize("path", ["srv/immich", "~/docker/immich", "/srv/a\nb"])
    def test_the_directory_must_be_absolute(self, path):
        with pytest.raises(validate.ValidationError, match="absolute path"):
            self.entry(path=path)

    def test_the_host_must_exist_and_run_docker(self):
        with pytest.raises(validate.ValidationError, match="configured servers"):
            self.entry(server="nope")
        with pytest.raises(validate.ValidationError, match="does not run Docker"):
            self.entry(server="router")

    def test_urls_are_normalised_and_checked(self):
        entry = self.entry(web_url="photos.lan", health_url="http://10.0.0.6:2283/api/ping", on_demand="on")
        assert entry["web_url"] == "https://photos.lan" and entry["on_demand"] is True
        with pytest.raises(validate.ValidationError, match="Health check"):
            self.entry(health_url="javascript:alert(1)//")

    def test_the_form_owns_exactly_what_it_writes(self):
        entry = self.entry(web_url="a.lan", health_url="b.lan", on_demand="on",
                           update="custom", update_cmd="docker compose up -d --build",
                           update_timeout="900")
        assert set(entry) == validate.CONTAINER_FIELDS

    def test_pull_is_the_default_and_is_not_written(self):
        assert "update" not in self.entry(update="pull") and "update" not in self.entry()
        assert self.entry(update="skip")["update"] == "skip"

    def test_a_custom_update_needs_its_command(self):
        with pytest.raises(validate.ValidationError, match="needs a command"):
            self.entry(update="custom")
        with pytest.raises(validate.ValidationError, match="Update must be one of"):
            self.entry(update="rebuild")


class TestAct:
    def test_runs_compose_in_the_quoted_directory(self, monkeypatch):
        sent = []

        class FakeSSH:
            def __enter__(self): return self
            def __exit__(self, *a): return False
        monkeypatch.setattr(containers, "connect", lambda *a, **kw: FakeSSH())
        monkeypatch.setattr(containers, "run", lambda ssh, cmd, timeout: sent.append(cmd) or ("ok", "", 0))
        monkeypatch.setattr(containers, "self_container_id", lambda: None)
        monkeypatch.setattr(containers.config, "resolve_ssh_key", lambda s: None)
        servers = [{"name": "h", "host": "10.0.0.1", "user": "op", "platform": "linux"}]
        containers.act({"name": "x", "server": "h", "path": "/srv/my app; rm -rf /"}, servers, "start")
        assert "cd '/srv/my app; rm -rf /' && $D compose up -d" in sent[0]

    def test_refuses_to_stop_timar_itself(self, monkeypatch):
        me = "f" * 64
        monkeypatch.setattr(containers, "self_container_id", lambda: me)
        monkeypatch.setattr(containers, "_host_containers", lambda server: containers.HostContainers(
            by_dir=containers.parse_ps(ps_line("/srv/timar", "timar", cid=me))))
        servers = [{"name": "h", "host": "10.0.0.1", "user": "op", "platform": "linux"}]
        with pytest.raises(containers.ContainerError, match="timar itself"):
            containers.act({"name": "timar", "server": "h", "path": "/srv/timar"}, servers, "stop")


def test_self_container_id_comes_from_mountinfo(monkeypatch, tmp_path):
    cid = "ab" * 32
    fake = tmp_path / "mountinfo"
    fake.write_text(f"1 2 0:1 /var/lib/docker/containers/{cid}/hostname /etc/hostname rw\n")
    real = containers.Path
    monkeypatch.setattr(containers, "Path", lambda p: fake if p == "/proc/self/mountinfo" else real(p))
    assert containers.self_container_id() == cid


class TestAppLogo:
    """The light before a project's name: its application's logo, read off names timar already
    has. Project names are often not the application's, so the images usually decide."""

    @pytest.mark.parametrize("name, images, logo", [
        ("git", ("codeberg.org/forgejo/forgejo:11",), "forgejo"),
        ("vault", ("vaultwarden/server:1.34",), "vaultwarden"),             # the owner names it
        ("speedtest", ("lscr.io/linuxserver/speedtest-tracker:latest",), "speedtest"),
        ("ollama-gateway", ("ollama/ollama:latest",), "ollama"),
        ("traefik", ("traefik:v3",), "traefikproxy"),                         # an alias
        ("db", ("postgres:16",), "postgresql"),
        ("x", ("localhost:5000/grafana/grafana:12@sha256:" + "0" * 64,), "grafana"),  # registry, digest
    ])
    def test_names_and_images(self, name, images, logo):
        assert containers.app_logo(name, images) == logo

    def test_the_application_beats_the_database_it_ships_with(self):
        """docker ps lists a project's containers in no promised order."""
        images = ("docker.io/valkey/valkey:8", "ghcr.io/immich-app/postgres:14",
                  "ghcr.io/immich-app/immich-server:v2")
        assert containers.app_logo("photos", images) == "immich"

    def test_a_project_that_builds_its_own_image_is_not_its_sidecar(self):
        """Compose names a locally built image `<project>-<service>`: that project is its own
        application, and the search engine it runs beside it is not its face."""
        images = ("searxng/searxng:latest", "app-app-frontend", "app-app-backend")
        assert containers.app_logo("app", images) == "docker"
        # Without its own build, the same sidecar does name the project.
        assert containers.app_logo("search", ("searxng/searxng:latest",)) == "searxng"

    @pytest.mark.parametrize("name, images", [
        ("timar", ("ghcr.io/example/timar:0.2",)),
        ("batch-tools", ("batch-tools:local",)),
        ("brain", ("68b2a218869a",)),                  # an image known only by its id
    ])
    def test_a_local_build_is_the_whale(self, name, images):
        assert containers.app_logo(name, images) == "docker"

    def test_a_project_with_no_images_to_read_keeps_its_last_logo(self, tmp_path, monkeypatch):
        """Asleep host, or never created: the row must not turn into the whale until it is back."""
        import importlib
        monkeypatch.setenv("TIMAR_DATA", str(tmp_path))
        from timar import config
        importlib.reload(config)
        assert containers._remembered_logo("h:/srv/photos", "photos", ("immich-app/immich-server:v2",)) == "immich"
        assert containers._remembered_logo("h:/srv/photos", "photos", ()) == "immich"
        assert containers._remembered_logo("h:/srv/other", "other", ()) == "docker"


class TestShortImage:
    """The images column shows what runs and at which tag; the full reference is the tooltip."""

    @pytest.mark.parametrize("image, shown", [
        ("ghcr.io/immich-app/immich-server:v2", "immich-server:v2"),
        ("ghcr.io/immich-app/postgres:14@sha256:" + "a" * 64, "postgres:14"),     # digest dropped
        ("vaultwarden/server:1.34", "server:1.34"),
        ("traefik:v3", "traefik:v3"),
        ("localhost:5000/team/app:1.2", "app:1.2"),       # a registry port is not a tag
        ("app-app-backend", "app-app-backend"),            # a local build, untouched
        ("sha256:" + "68b2a218869a" + "0" * 52, "sha256:68b2a218869a"),   # known only by its id
    ])
    def test_shown(self, image, shown):
        assert containers.short_image(image) == shown


class TestFloatingTag:
    """A tag the update run moves is marked; a pinned one, an id or a local build is not."""

    @pytest.mark.parametrize("image, floating", [
        ("traefik:latest", True),
        ("vaultwarden/server:latest", True),
        ("ollama/ollama", True),                         # from a registry, no tag: latest
        ("localhost:5000/team/app", True),               # a registry port is not a tag
        ("ghcr.io/immich-app/immich-server:v2", False),
        ("postgres:14@sha256:" + "b" * 64, False),
        ("app-app-backend", False),                      # a local build is not pulled
        ("sha256:" + "a" * 64, False),
    ])
    def test_which(self, image, floating):
        assert containers.is_floating(image) is floating


class TestPage:
    @pytest.fixture
    def page(self, client, monkeypatch):
        from timar import config
        complete_setup(client)
        config.save({"servers": [{"name": "docker-01", "host": "10.0.0.6", "user": "op", "platform": "linux"},
                                 {"name": "router", "host": "10.0.0.1", "user": "root", "platform": "openwrt"}],
                     "containers": [{"name": "immich", "server": "docker-01", "path": "/srv/immich",
                                     "web_url": "https://photos.lan"},
                                    {"name": "odata", "server": "docker-01", "path": "/srv/odata", "on_demand": True}]})
        monkeypatch.setattr(containers.fleet_status, "_probe", lambda host: True)
        monkeypatch.setattr(containers, "self_container_id", lambda: None)
        monkeypatch.setattr(containers, "_host_containers", lambda server: containers.HostContainers(
            by_dir=containers.parse_ps(ps_line("/srv/immich", "immich-server", image="immich:v2"))))
        containers.invalidate()
        return client

    def test_rows_show_state_link_and_actions(self, page):
        rows = page.get("/fragments/containers").text
        assert '<tr class="up">' in rows and '<tr class="asleep">' in rows
        assert '<a href="https://photos.lan" target="_blank"' in rows and "immich:v2" in rows
        assert "/containers/immich/stop" in rows and "/containers/immich/restart" in rows
        assert "/containers/odata/start" in rows
        assert 'hx-target="#toast"' in rows
        assert rows.count('href="/settings/containers/new"') == 1

    def test_the_light_is_the_project_s_logo_in_its_state_s_colour(self, page):
        rows = page.get("/fragments/containers").text
        up = rows.split('<tr class="up">', 1)[1].split("</td>", 1)[0]
        assert 'class="os"' in up and '#immich"></use>' in up and 'title="running · Immich"' in up
        # Nothing created yet and no images ever seen: the whale.
        asleep = rows.split('<tr class="asleep">', 1)[1].split("</td>", 1)[0]
        assert '#docker"></use>' in asleep and "· Docker" in asleep

    def test_an_image_is_shown_short_with_the_whole_reference_on_hover(self, client, monkeypatch):
        from timar import config
        complete_setup(client)
        config.save({"servers": [{"name": "docker-01", "host": "10.0.0.6", "user": "op", "platform": "linux"}],
                     "containers": [{"name": "photos", "server": "docker-01", "path": "/srv/photos"}]})
        monkeypatch.setattr(containers.fleet_status, "_probe", lambda host: True)
        monkeypatch.setattr(containers, "self_container_id", lambda: None)
        monkeypatch.setattr(containers, "_host_containers", lambda server: containers.HostContainers(
            by_dir=containers.parse_ps(ps_line("/srv/photos", "immich-server",
                                               image="ghcr.io/immich-app/immich-server:v2"))))
        containers.invalidate()
        rows = client.get("/fragments/containers").text
        assert '<code title="ghcr.io/immich-app/immich-server:v2">immich-server<span class="tag">:v2</span></code>' in rows

    def test_the_host_and_its_directory_are_on_two_lines(self, page):
        rows = page.get("/fragments/containers").text
        assert ">Host / Path<" in rows
        assert 'docker-01<div class="wide"><code>/srv/immich</code></div>' in rows

    def test_the_page_carries_the_menu_and_requires_a_session(self, page):
        assert 'aria-current="page">containers<' in page.get("/containers").text
        page.cookies.clear()
        assert page.get("/containers").headers["location"] == "/login"
        assert page.post("/containers/immich/stop").headers["location"] == "/login"

    def test_the_host_list_offers_only_docker_hosts(self, page):
        form = page.get("/settings/containers/new").text
        assert 'value="docker-01"' in form and 'value="router"' not in form

    def test_add_edit_and_remove(self, page):
        from timar import config
        response = page.post("/settings/containers", data={
            "name": "brain", "server": "docker-01", "path": "/srv/brain"})
        assert response.headers["location"] == "/containers"
        page.post("/settings/containers", data={
            "original_name": "brain", "name": "brain2", "server": "docker-01", "path": "/srv/brain"})
        assert [c["name"] for c in config.load()["containers"]] == ["immich", "odata", "brain2"]
        page.post("/settings/containers/brain2/delete")
        assert [c["name"] for c in config.load()["containers"]] == ["immich", "odata"]

    def test_an_edit_keeps_keys_the_form_does_not_own(self, page):
        from timar import config
        cfg = config.load()
        cfg["containers"][0]["notes"] = "written by hand"
        config.save(cfg)
        page.post("/settings/containers", data={
            "original_name": "immich", "name": "immich", "server": "docker-01", "path": "/srv/immich"})
        assert config.load()["containers"][0]["notes"] == "written by hand"

    def test_a_server_rename_follows_into_its_containers(self, page):
        from timar import config
        page.post("/settings/servers", data={
            "original_name": "docker-01", "name": "docker-02", "host": "10.0.0.6", "user": "op",
            "platform": "linux"})
        assert {c["server"] for c in config.load()["containers"]} == {"docker-02"}

    def test_an_action_answers_as_a_sentence_and_errors_are_escaped(self, page, monkeypatch):
        monkeypatch.setattr(containers, "act", lambda entry, servers, action: f"{entry['name']}: {action} done")
        assert "immich: restart done" in page.post("/containers/immich/restart").text

        def fail(*a):
            raise containers.ContainerError("<b>compose</b> failed")
        monkeypatch.setattr(containers, "act", fail)
        body = page.post("/containers/immich/stop").text
        assert "<b>" not in body and "&lt;b&gt;" in body
        assert page.post("/containers/immich/explode").status_code == 404
        assert page.post("/containers/nope/stop").status_code == 404


class TestUpdateCommand:
    def test_pull_then_up_only_for_a_running_project_and_never_down(self):
        running = containers.update_command({"path": "/srv/a b"}, running=True)
        assert "cd '/srv/a b' && $D compose pull --ignore-buildable && $D compose up -d" in running
        stopped = containers.update_command({"path": "/srv/a"}, running=False)
        assert stopped.endswith("$D compose pull --ignore-buildable")
        assert "down" not in running + stopped

    def test_custom_runs_in_the_directory(self):
        cmd = containers.update_command({"path": "/srv/a", "update": "custom",
                                         "update_cmd": "docker compose up -d --build"}, running=True)
        assert cmd.endswith("cd /srv/a && docker compose up -d --build")


class TestUpdateRun:
    """The update run's container half, against a fake SSH session."""

    def run_it(self, monkeypatch, entries, ps_output, me=None, fail=()):
        from timar import updater
        sent = []

        def fake_run(ssh, cmd, timeout=120):
            sent.append(cmd)
            return (ps_output, "", 0) if "ps -a" in cmd else ("", "", 0)

        def fake_update(ssh, cmd, timeout):
            sent.append(cmd)
            return (False, "pull access denied") if any(f in cmd for f in fail) else (True, "")
        monkeypatch.setattr(updater, "run", fake_run)
        monkeypatch.setattr(updater, "_do_update", fake_update)
        monkeypatch.setattr(containers, "self_container_id", lambda: me)
        results = updater._update_containers(object(), {"name": "docker-01"}, entries)
        return results, sent

    def test_each_project_gets_its_own_result_and_the_host_is_pruned_once(self, monkeypatch):
        ps = "\n".join([ps_line("/srv/immich", "immich-server"),
                         ps_line("/srv/odata", "odata-1", "exited", "Exited (0) 2 days ago")])
        results, sent = self.run_it(monkeypatch, [
            {"name": "immich", "path": "/srv/immich"},
            {"name": "odata", "path": "/srv/odata", "on_demand": True},
            {"name": "brain", "path": "/srv/brain", "update": "skip"},
            {"name": "bad", "path": "/srv/bad"},
        ], ps, fail=("/srv/bad",))
        by = {r.server: r for r in results}
        assert by["immich (docker-01)"].success and not by["immich (docker-01)"].note
        assert by["odata (docker-01)"].note == "pulled, left stopped"
        assert by["brain (docker-01)"].skipped
        assert not by["bad (docker-01)"].success and "pull access denied" in by["bad (docker-01)"].error
        assert sum("image prune" in c for c in sent) == 1
        assert any("cd /srv/immich && $D compose pull --ignore-buildable && $D compose up -d" in c for c in sent)
        assert not any("cd /srv/odata" in c and "up -d" in c for c in sent)

    def test_timar_never_updates_itself(self, monkeypatch):
        me = "e" * 64
        results, sent = self.run_it(monkeypatch, [{"name": "timar", "path": "/srv/timar"}],
                                    ps_line("/srv/timar", "timar", cid=me), me=me)
        assert results[0].skipped and "timar itself" in results[0].error
        assert not any("cd /srv/timar" in c for c in sent)

    def test_run_updates_hands_each_host_its_own_projects(self, monkeypatch):
        from timar import updater
        seen = {}
        monkeypatch.setattr(updater, "update_server",
                            lambda server, servers_map, by_server, cfg=None, **kw: seen.update(by_server) or [])
        monkeypatch.setattr(updater.membership, "skip_reason", lambda cfg, job, server: None)
        updater.run_updates({"servers": [{"name": "a"}, {"name": "b"}],
                             "containers": [{"name": "x", "server": "a", "path": "/x"},
                                            {"name": "y", "server": "b", "path": "/y"},
                                            {"name": "z", "server": "a", "path": "/z"}]})
        assert [e["name"] for e in seen["a"]] == ["x", "z"] and [e["name"] for e in seen["b"]] == ["y"]


class TestLogSweep:
    def test_stopped_containers_of_on_demand_projects_are_not_findings(self):
        from timar.log_checker import stopped_containers
        out = "\n".join([
            ps_line("/srv/odata", "odata-1", "exited", "Exited (0) 1 day ago"),
            ps_line("/srv/immich", "immich-db", "exited", "Exited (1) 1 hour ago"),
            json.dumps({"ID": "loose", "Names": "one-off", "State": "exited", "Labels": ""}),
        ])
        assert stopped_containers(out, frozenset({"/srv/odata"})) == ["immich-db", "one-off"]
        assert sorted(stopped_containers(out)) == ["immich-db", "odata-1", "one-off"]


class TestDiscovery:
    LS = json.dumps([
        {"Name": "immich", "Status": "running(4)", "ConfigFiles": "/srv/immich/docker-compose.yml"},
        {"Name": "my app", "Status": "exited(1)", "ConfigFiles": "/srv/app/compose.yaml,/srv/app/override.yml"},
        {"Name": "odd", "Status": "running(1)", "ConfigFiles": "relative/compose.yml"},
    ])
    PS = json.dumps({"ID": "1", "Labels": "com.docker.compose.project.working_dir=/srv/immich,"
                     "traefik.http.routers.immich.rule=Host(`photos.lan`),"
                     "traefik.http.routers.immich.middlewares=a@file,b@file"})

    def test_parses_projects_with_a_guessed_web_address(self):
        found = containers.parse_discovery(self.LS, self.PS)
        assert [(f.name, f.path, f.web_url) for f in found] == [
            ("immich", "/srv/immich", "https://photos.lan"), ("my-app", "/srv/app", None)]

    def test_garbage_is_no_projects_not_a_crash(self):
        assert containers.parse_discovery("not json", "") == []


class TestImport:
    @pytest.fixture
    def page(self, client, monkeypatch):
        from timar import config
        complete_setup(client)
        config.save({"servers": [{"name": "docker-01", "host": "10.0.0.6", "user": "op", "platform": "linux"}],
                     "containers": [{"name": "immich", "server": "docker-01", "path": "/srv/immich"}]})
        monkeypatch.setattr(containers, "discover", lambda server: containers.parse_discovery(
            TestDiscovery.LS, TestDiscovery.PS))
        return client

    def test_offers_only_what_is_not_registered(self, page):
        body = page.get("/settings/containers/discover?server=docker-01",
                        headers={"HX-Request": "true"}).text
        assert "/srv/app" in body and "/srv/immich" not in body
        assert 'hx-target="#discover-result"' in body

    def test_the_add_form_has_the_finder_and_the_edit_form_does_not(self, page):
        assert 'id="discover-result"' in page.get("/settings/containers/new").text
        assert 'id="discover-result"' not in page.get("/settings/containers/immich/edit").text

    def test_ticked_rows_are_added_with_their_edits(self, page):
        from timar import config
        response = page.post("/settings/containers/import", data={
            "server": "docker-01", "pick": ["0"], "name_0": "my-app", "path_0": "/srv/app",
            "web_0": "app.lan"})
        assert response.headers["location"] == "/containers"
        assert config.load()["containers"][-1] == {
            "name": "my-app", "server": "docker-01", "path": "/srv/app", "web_url": "https://app.lan"}

    def test_one_bad_row_adds_nothing_and_comes_back_as_sent(self, page):
        from timar import config
        response = page.post("/settings/containers/import", data={
            "server": "docker-01", "pick": ["0", "1"],
            "name_0": "immich", "path_0": "/srv/app", "web_0": "",          # name taken
            "name_1": "fine", "path_1": "/srv/fine", "web_1": ""})
        assert response.status_code == 400
        assert "already exists" in response.text
        assert [c["name"] for c in config.load()["containers"]] == ["immich"]

    def test_nothing_ticked_is_said_so(self, page):
        response = page.post("/settings/containers/import", data={"server": "docker-01"})
        assert response.status_code == 400 and "Tick at least one" in response.text

    def test_a_host_that_cannot_be_asked_says_why(self, page, monkeypatch):
        def fail(server):
            raise containers.ContainerError("could not reach docker-01: <timed out>")
        monkeypatch.setattr(containers, "discover", fail)
        body = page.get("/settings/containers/discover?server=docker-01").text
        assert "could not reach docker-01" in body and "<timed out>" not in body
        assert page.get("/settings/containers/discover?server=nope").status_code == 404


class TestStop:
    def test_the_update_run_stops_between_hosts_and_says_so(self, monkeypatch):
        from timar import cancel, jobs, updater
        visited = []

        monkeypatch.setattr(updater.membership, "skip_reason", lambda cfg, job, server: None)

        def fake_update_server(server, servers_map, by_server, cfg=None, **kw):
            visited.append(server["name"])
            cancel.request("update")      # the operator presses stop during the first host
            return [updater.UpdateResult(server=server["name"], success=True)]
        monkeypatch.setattr(updater, "update_server", fake_update_server)
        monkeypatch.setattr(jobs, "run_updates", updater.run_updates)
        monkeypatch.setattr(jobs, "_notify", lambda cfg, text: None)
        try:
            outcome = jobs.run_update({"servers": [{"name": "a"}, {"name": "b"}, {"name": "c"}]})
        finally:
            cancel.clear("update")
        assert visited == ["a"]
        assert "stopped by the operator" in outcome.summary and "not reached" in outcome.report

    def test_a_stop_between_projects_still_prunes_and_returns(self, monkeypatch):
        from timar import cancel, updater
        monkeypatch.setattr(containers, "self_container_id", lambda: None)
        calls = []

        def fake_update(ssh, cmd, timeout):
            calls.append(cmd)
            cancel.request("update")
            return True, ""
        monkeypatch.setattr(updater, "run", lambda ssh, cmd, timeout=120: calls.append(cmd) or ("", "", 0))
        monkeypatch.setattr(updater, "_do_update", fake_update)
        try:
            results = updater._update_containers(object(), {"name": "h"}, [
                {"name": "a", "path": "/a"}, {"name": "b", "path": "/b"}])
        finally:
            cancel.clear("update")
        assert [r.server for r in results] == ["a (h)"]
        assert any("image prune" in c for c in calls)


class TestBackgroundStatus:
    """Pages read the last answer; the background task is what asks."""

    def test_a_request_reads_the_cached_answer_without_probing(self, monkeypatch):
        from timar import status
        calls = []
        monkeypatch.setattr(status, "is_host_up", lambda host, timeout: calls.append(host) or True)
        status.invalidate()
        status.refresh({"servers": [{"name": "a", "host": "10.0.0.1"}]})
        assert calls == ["10.0.0.1"]
        assert status._probe("10.0.0.1") is True and calls == ["10.0.0.1"]   # no second probe

    def test_an_answer_older_than_stale_after_is_asked_again(self, monkeypatch):
        from timar import status
        calls = []
        monkeypatch.setattr(status, "is_host_up", lambda host, timeout: calls.append(host) or False)
        status.invalidate()
        status._cache["10.0.0.2"] = (status.time.monotonic() - status.STALE_AFTER - 1, True)
        assert status._probe("10.0.0.2") is False and calls == ["10.0.0.2"]

    def test_refresh_always_asks_again(self, monkeypatch):
        from timar import status
        answers = iter([True, False])
        monkeypatch.setattr(status, "is_host_up", lambda host, timeout: next(answers))
        status.invalidate()
        cfg = {"servers": [{"name": "a", "host": "10.0.0.3"}]}
        status.refresh(cfg)
        status.refresh(cfg)
        assert status._probe("10.0.0.3") is False

    def test_the_probe_gives_an_off_machine_one_second(self):
        from timar import status
        assert status.PROBE_TIMEOUT == 1.0

    def test_container_refresh_skips_hosts_that_are_off(self, monkeypatch):
        from timar import status
        asked = []
        monkeypatch.setattr(status, "is_host_up", lambda host, timeout: host == "10.0.0.6")
        monkeypatch.setattr(containers, "_ps", lambda server: asked.append(server["name"]) or containers.HostContainers())
        monkeypatch.setattr(containers, "_healthy", lambda url, fresh=False: asked.append(url) or True)
        status.invalidate(); containers.invalidate()
        cfg = {"servers": [{"name": "on", "host": "10.0.0.6"}, {"name": "off", "host": "10.0.0.7"}],
               "containers": [{"name": "a", "server": "on", "path": "/a", "health_url": "https://a.lan"},
                              {"name": "b", "server": "off", "path": "/b", "health_url": "https://b.lan"}]}
        status.refresh(cfg)
        containers.refresh(cfg)
        assert sorted(asked) == ["https://a.lan", "on"]


class TestSort:
    def test_by_name_and_by_host_both_ways(self):
        mk = lambda name, server, path: containers.ProjectStatus(
            name=name, server=server, path=path, state="up", detail="", images=(), on_demand=False,
            web_url=None, is_self=False)
        ps = [mk("b", "h2", "/b"), mk("a", "h2", "/z"), mk("c", "h1", "/c")]
        assert [p.name for p in containers.sort_projects(ps, "name")] == ["a", "b", "c"]
        assert [p.name for p in containers.sort_projects(ps, "host")] == ["c", "b", "a"]
        assert [p.name for p in containers.sort_projects(ps, "host", True)] == ["a", "b", "c"]

    def test_the_headings_sort_and_the_poll_keeps_the_order(self, client, monkeypatch):
        from timar import config
        complete_setup(client)
        config.save({"servers": [{"name": "h", "host": "10.0.0.6", "user": "op", "platform": "linux"}],
                     "containers": [{"name": "b", "server": "h", "path": "/b"},
                                    {"name": "a", "server": "h", "path": "/a"}]})
        monkeypatch.setattr(containers.fleet_status, "_probe", lambda host, fresh=False: False)
        page = client.get("/containers?sort=host&dir=desc").text
        assert 'href="/containers?sort=host&amp;dir=asc"' in page        # active column flips
        assert 'href="/containers?sort=name&amp;dir=asc"' in page
        assert 'hx-get="/fragments/containers?sort=host&amp;dir=desc"' in page
        rows = client.get("/fragments/containers?sort=name&dir=desc").text
        assert rows.index('href="/settings/containers/b/edit"') < rows.index('href="/settings/containers/a/edit"')
