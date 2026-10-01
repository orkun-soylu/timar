"""Which servers a job runs on: enrolled, and not left out of it."""
import pytest

from timar import config, membership
from tests.test_web import client, complete_setup  # noqa: F401 — the fixture


@pytest.fixture(autouse=True)
def data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("TIMAR_DATA", str(tmp_path))
    membership.reset_cache()
    yield tmp_path
    membership.reset_cache()


def write_known_hosts(tmp_path, *names):
    (tmp_path / "ssh").mkdir(exist_ok=True)
    (tmp_path / "ssh" / "known_hosts").write_text(
        "".join(f"{n} ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample\n" for n in names))


class TestEnrolled:
    def test_the_first_read_starts_from_known_hosts(self, data_dir):
        write_known_hosts(data_dir, "10.0.0.1", "[127.0.0.1]:2345")
        assert membership.enrolled() == {"10.0.0.1", "[127.0.0.1]:2345"}
        assert (data_dir / "ssh" / "enrolled").read_text() == "10.0.0.1\n[127.0.0.1]:2345\n"

    def test_an_address_with_a_port_matches_its_known_hosts_form(self, data_dir):
        write_known_hosts(data_dir, "[127.0.0.1]:2345")
        assert membership.is_enrolled({"host": "127.0.0.1:2345"})
        assert not membership.is_enrolled({"host": "127.0.0.1"})

    def test_a_key_login_records_the_address_once(self, data_dir):
        membership.mark_enrolled("10.0.0.9")
        membership.mark_enrolled("10.0.0.9")
        assert (data_dir / "ssh" / "enrolled").read_text().count("10.0.0.9") == 1
        membership.reset_cache()
        assert membership.is_enrolled({"host": "10.0.0.9"})

    def test_ssh_connect_marks_the_address(self, data_dir, monkeypatch):
        import paramiko
        from timar import ssh
        monkeypatch.setattr(paramiko.SSHClient, "connect", lambda self, **kw: None)
        with ssh.connect("10.0.0.7:2222", "op", None):
            pass
        assert membership.is_enrolled({"host": "10.0.0.7:2222"})


class TestLists:
    CFG = {"servers": [{"name": "c", "host": "10.0.0.3"}, {"name": "a", "host": "10.0.0.1"},
                       {"name": "b", "host": "10.0.0.2"}],
           "job_exclude": {"update": ["b"]}}

    def test_partition(self, data_dir):
        write_known_hosts(data_dir, "10.0.0.1", "10.0.0.2")
        runs, left, unenrolled = membership.lists(self.CFG, "update")
        assert [s["name"] for s in runs] == ["a"]
        assert [s["name"] for s in left] == ["b"]
        assert [s["name"] for s in unenrolled] == ["c"]
        # The other job is untouched by update's list.
        assert [s["name"] for s in membership.lists(self.CFG, "log_sweep")[0]] == ["a", "b"]

    def test_reasons(self, data_dir):
        write_known_hosts(data_dir, "10.0.0.1", "10.0.0.2")
        servers = {s["name"]: s for s in self.CFG["servers"]}
        assert membership.skip_reason(self.CFG, "update", servers["a"]) is None
        assert "left out" in membership.skip_reason(self.CFG, "update", servers["b"])
        assert "not enrolled" in membership.skip_reason(self.CFG, "update", servers["c"])

    def test_leave_join_rename_forget(self):
        cfg = {"servers": []}
        membership.set_excluded(cfg, "update", "x", leave_out=True)
        membership.set_excluded(cfg, "update", "y", leave_out=True)
        membership.set_excluded(cfg, "update", "y", leave_out=False)
        assert cfg["job_exclude"] == {"update": ["x"]}
        membership.rename(cfg, "x", "x2")
        assert cfg["job_exclude"] == {"update": ["x2"]}
        membership.forget(cfg, "x2")
        assert cfg["job_exclude"] == {"update": []}


class TestJobsHonourIt:
    def test_the_update_run_skips_a_left_out_hypervisor_and_its_vms(self, data_dir, monkeypatch):
        from timar import updater
        write_known_hosts(data_dir, "10.0.0.1", "10.0.0.2", "10.0.0.3")
        visited = []
        monkeypatch.setattr(updater, "update_server",
                            lambda server, m, b, cfg=None: visited.append(server["name"]) or [])
        results = updater.run_updates({
            "servers": [{"name": "hv", "host": "10.0.0.1", "manages_vms": [{"vm_id": 1, "server_name": "vm"}]},
                        {"name": "vm", "host": "10.0.0.2"},
                        {"name": "web", "host": "10.0.0.3"},
                        {"name": "new", "host": "10.0.0.4"}],
            "job_exclude": {"update": ["hv"]}})
        assert visited == ["web"]
        reasons = {r.server: r.error for r in results}
        assert reasons["hv"].startswith("left out") and "hypervisor hv" in reasons["vm"]
        assert "not enrolled" in reasons["new"]
        assert all(r.skipped and not r.was_running for r in results)

    def test_the_sweep_leaves_them_out_and_counts_them(self, data_dir, monkeypatch):
        from timar import jobs, log_checker
        write_known_hosts(data_dir, "10.0.0.1")
        checked = []
        monkeypatch.setattr(log_checker, "check_server", lambda s, h, t, d=frozenset(): checked.append(s["name"])
                            or log_checker.LogResult(server=s["name"], success=True, offline=True))
        monkeypatch.setattr(jobs, "_notify", lambda cfg, text: None)
        monkeypatch.setattr(jobs.analysis, "analyze", lambda *a, **kw: None)
        outcome = jobs.run_log_sweep({"servers": [{"name": "a", "host": "10.0.0.1"},
                                                  {"name": "b", "host": "10.0.0.2"}]})
        assert checked == ["a"] and "1 not in the sweep" in outcome.summary


class TestDialog:
    @pytest.fixture
    def page(self, client, data_dir):
        complete_setup(client)
        write_known_hosts(data_dir, "10.0.0.1", "10.0.0.2")
        config.save({"servers": [{"name": "a", "host": "10.0.0.1", "user": "op"},
                                 {"name": "b", "host": "10.0.0.2", "user": "op"},
                                 {"name": "c", "host": "10.0.0.3", "user": "op"}]})
        return client

    def test_the_dialog_lists_three_sections(self, page):
        body = page.get("/settings/jobs/update/edit", headers={"HX-Request": "true"}).text
        runs = body.split("<h3>Runs on</h3>", 1)[1].split("<h3>Left out</h3>", 1)[0]
        assert "/hosts/a/leave" in runs and "/hosts/b/leave" in runs and "hx-confirm" in runs
        unenrolled = body.split("<h3>Not enrolled</h3>", 1)[1]
        assert 'hx-get="/settings/servers/c/edit"' in unenrolled
        assert body.index('id="job-hosts"') < body.index("form-actions")

    def test_leave_and_join_are_saved_and_answer_with_the_lists(self, page):
        lists = page.post("/settings/jobs/update/hosts/a/leave").text
        assert config.load()["job_exclude"] == {"update": ["a"]}
        assert "/hosts/a/join" in lists.split("<h3>Left out</h3>", 1)[1]
        assert "<form" not in lists                     # only the lists, the schedule is untouched
        page.post("/settings/jobs/update/hosts/a/join")
        assert "job_exclude" not in config.load()

    def test_unknown_job_server_or_action_is_404(self, page):
        assert page.post("/settings/jobs/nope/hosts/a/leave").status_code == 404
        assert page.post("/settings/jobs/update/hosts/nope/leave").status_code == 404
        assert page.post("/settings/jobs/update/hosts/a/delete").status_code == 404

    def test_a_rename_and_a_removal_follow_into_the_lists(self, page):
        page.post("/settings/jobs/update/hosts/a/leave")
        page.post("/settings/servers", data={"original_name": "a", "name": "a2", "host": "10.0.0.1",
                                             "user": "op", "platform": "linux"})
        assert config.load()["job_exclude"] == {"update": ["a2"]}
        page.post("/settings/servers/a2/delete")
        assert not (config.load().get("job_exclude") or {}).get("update")
