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
        with pytest.raises(containers.ContainerError, match="Timar itself"):
            containers.act({"name": "timar", "server": "h", "path": "/srv/timar"}, servers, "stop")


def test_self_container_id_comes_from_mountinfo(monkeypatch, tmp_path):
    cid = "ab" * 32
    fake = tmp_path / "mountinfo"
    fake.write_text(f"1 2 0:1 /var/lib/docker/containers/{cid}/hostname /etc/hostname rw\n")
    real = containers.Path
    monkeypatch.setattr(containers, "Path", lambda p: fake if p == "/proc/self/mountinfo" else real(p))
    assert containers.self_container_id() == cid


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
        assert rows.count('href="/settings/containers/new"') == 1

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
        assert results[0].skipped and "Timar itself" in results[0].error
        assert not any("cd /srv/timar" in c for c in sent)

    def test_run_updates_hands_each_host_its_own_projects(self, monkeypatch):
        from timar import updater
        seen = {}
        monkeypatch.setattr(updater, "update_server",
                            lambda server, servers_map, by_server: seen.update(by_server) or [])
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
