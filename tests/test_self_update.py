"""Updating the host timar itself runs on.

The run that motivated this: an update run upgraded docker-ce on that host as its first step,
dockerd restarted, the container went with it twenty seconds in, and the whole run was recorded
as "Interrupted" — every other host untouched, and apt left running as the child of an SSH
session whose other end had just died. So the host goes last, its containers before its
packages, and its package update runs under the host's systemd rather than this session.
"""
import shlex
import subprocess

import pytest

from timar import containers, jobs, updater
from timar.updater import UpdateResult

ME = "a" * 64


def fleet():
    """Config order puts timar's own host first — the order that killed the real run."""
    return {
        "servers": [
            {"name": "docker-01", "host": "10.0.0.6", "user": "op", "platform": "linux"},
            {"name": "hv-00", "host": "10.0.0.5", "user": "root", "platform": "proxmox"},
            {"name": "web-01", "host": "10.0.0.7", "user": "op", "platform": "linux"},
        ],
        "containers": [{"name": "timar", "server": "docker-01", "path": "/srv/timar"},
                       {"name": "app", "server": "docker-01", "path": "/srv/app"}],
    }


@pytest.fixture
def no_membership(monkeypatch):
    monkeypatch.setattr(updater.membership, "skip_reason", lambda cfg, job, server: None)


class TestOrder:
    def test_timar_s_own_host_goes_last_and_the_rest_keep_config_order(self, monkeypatch, no_membership):
        """Everything that is woken and shut down again is finished before the one step that
        may restart this process — an interruption there then leaves no machine awake."""
        order = []
        monkeypatch.setattr(updater, "find_self_host", lambda cfg: "docker-01")
        monkeypatch.setattr(updater, "update_server",
                            lambda server, *a, **kw: order.append(server["name"]) or [])
        monkeypatch.setattr(updater, "update_self",
                            lambda server, entries, done, on_handoff: order.append(server["name"]) or [])
        updater.run_updates(fleet())
        assert order == ["hv-00", "web-01", "docker-01"]

    def test_without_a_host_of_its_own_nothing_moves(self, monkeypatch, no_membership):
        """Not in a container, or its project not registered: config order, as before."""
        order = []
        monkeypatch.setattr(updater, "find_self_host", lambda cfg: None)
        monkeypatch.setattr(updater, "update_server",
                            lambda server, *a, **kw: order.append(server["name"]) or [])
        monkeypatch.setattr(updater, "update_self", lambda *a: pytest.fail("no own host to update"))
        updater.run_updates(fleet())
        assert order == ["docker-01", "hv-00", "web-01"]

    def test_a_guest_vm_that_is_timar_s_host_is_kept_from_its_hypervisor(self, monkeypatch, no_membership):
        """When timar runs in a VM its hypervisor starts for updates, the hypervisor must leave
        that VM alone and the end of the run must take it."""
        cfg = fleet()
        cfg["servers"][1]["manages_vms"] = [{"vm_id": 300, "server_name": "docker-01"}]
        deferred, last = [], []
        monkeypatch.setattr(updater, "find_self_host", lambda cfg: "docker-01")
        monkeypatch.setattr(updater, "update_server",
                            lambda server, *a, defer=None, **kw: deferred.append((server["name"], defer)) or [])
        monkeypatch.setattr(updater, "update_self",
                            lambda server, *a: last.append(server["name"]) or [])
        updater.run_updates(cfg)
        assert deferred == [("hv-00", "docker-01"), ("web-01", "docker-01")]
        assert last == ["docker-01"]

    def test_a_stopped_run_does_not_reach_timar_s_own_host(self, monkeypatch, no_membership):
        monkeypatch.setattr(updater, "find_self_host", lambda cfg: "docker-01")
        monkeypatch.setattr(updater.cancel, "requested", lambda job: True)
        monkeypatch.setattr(updater, "update_self", lambda *a: pytest.fail("the run was stopped"))
        updater.run_updates(fleet())


class FakeSSH:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestFindingItsHost:
    def test_the_host_whose_docker_knows_this_container_is_the_one(self, monkeypatch):
        asked = []

        def fake_run(ssh, cmd, timeout=120):
            asked.append(ssh)
            return (ME + "\n", "", 0) if ssh == "docker-01" else ("", "No such object", 1)

        monkeypatch.setattr(containers, "self_container_id", lambda: ME)
        monkeypatch.setattr(updater, "is_host_up", lambda host, **kw: True)
        monkeypatch.setattr(updater, "resolve_ssh_key", lambda s: "key")
        cfg = fleet()
        cfg["containers"].insert(0, {"name": "x", "server": "web-01", "path": "/srv/x"})
        hosts = {s["host"]: s["name"] for s in cfg["servers"]}

        class Conn(FakeSSH):
            def __init__(self, host, user, key):
                self.name = hosts[host]

            def __enter__(self):
                return self.name

        monkeypatch.setattr(updater, "connect", Conn)
        monkeypatch.setattr(updater, "run", fake_run)
        assert updater.find_self_host(cfg) == "docker-01"
        # Only the hosts with registered projects are asked, each once.
        assert asked == ["web-01", "docker-01"]

    def test_outside_a_container_no_host_is_asked(self, monkeypatch):
        monkeypatch.setattr(containers, "self_container_id", lambda: None)
        monkeypatch.setattr(updater, "connect", lambda *a, **kw: pytest.fail("nothing to ask"))
        assert updater.find_self_host(fleet()) is None


class TestHandOff:
    def test_the_command_survives_the_session_that_started_it(self):
        cmd = updater.self_update_command("sudo apt-get -y upgrade && echo 'done'", "timar-self-update-1")
        assert "systemd-run" in cmd and "--collect" in cmd and "--unit=timar-self-update-1" in cmd
        # The delay is what lets the run be recorded before dockerd can restart; without the
        # accuracy setting systemd may hold it back by up to a minute more.
        assert f"--on-active={updater.HANDOFF_DELAY}" in cmd
        assert "AccuracySec=1s" in cmd
        # A stale status file from last week must not be read as this run's result.
        assert cmd.index(f"rm -f {updater.SELF_RC}") < cmd.index("systemd-run")

    def test_the_wrapped_command_records_its_own_exit_status(self, tmp_path, monkeypatch):
        """Run the inner script for real: quoting the operator's command into `sh -c` is where
        the WOL relay once lost a value, and only a shell can say whether this one survives."""
        monkeypatch.setattr(updater, "SELF_LOG", str(tmp_path / "log"))
        monkeypatch.setattr(updater, "SELF_RC", str(tmp_path / "rc"))
        cmd = updater.self_update_command("echo \"it's\" here && exit 7", "u")
        inner = shlex.split(cmd[cmd.index("/bin/sh -c"):])[2]
        subprocess.run(["/bin/sh", "-c", inner], check=True)
        assert (tmp_path / "rc").read_text().strip() == "7"
        assert (tmp_path / "log").read_text().strip() == "it's here"

    def test_containers_are_updated_before_the_packages_and_the_run_is_saved_first(self, monkeypatch):
        """A dockerd restart stops this process: a project left until after it would be updated
        by nobody, and results not saved before it would be lost with the process."""
        events = []
        monkeypatch.setattr(updater, "connect", lambda *a, **kw: FakeSSH())
        monkeypatch.setattr(updater, "resolve_ssh_key", lambda s: "key")
        monkeypatch.setattr(updater, "_update_containers",
                            lambda ssh, server, entries: events.append("containers")
                            or [UpdateResult(server="app (docker-01)", success=True)])
        monkeypatch.setattr(updater, "run", lambda ssh, cmd, timeout=120:
                            events.append("handed off") or ("", "", 0))
        monkeypatch.setattr(updater, "wait_for_self_update",
                            lambda server, unit, deadline, note="": events.append("waited")
                            or UpdateResult(server="docker-01", success=True))
        saved = []
        done = [UpdateResult(server="web-01", success=True)]
        results = updater.update_self(fleet()["servers"][0], [], done,
                                      lambda res, host, unit, deadline: events.append("saved")
                                      or saved.append([r.server for r in res]))
        assert events == ["containers", "handed off", "saved", "waited"]
        assert saved == [["web-01", "app (docker-01)"]]
        assert [r.server for r in results] == ["app (docker-01)", "docker-01"]

    def test_a_hand_off_that_systemd_refuses_is_a_failure_on_the_spot(self, monkeypatch):
        monkeypatch.setattr(updater, "connect", lambda *a, **kw: FakeSSH())
        monkeypatch.setattr(updater, "resolve_ssh_key", lambda s: "key")
        monkeypatch.setattr(updater, "_update_containers", lambda *a: [])
        monkeypatch.setattr(updater, "run", lambda *a, **kw: ("", "Unit already exists", 1))
        monkeypatch.setattr(updater, "wait_for_self_update", lambda *a, **kw: pytest.fail("nothing to wait for"))
        [result] = updater.update_self(fleet()["servers"][0], [], [],
                                       lambda *a: pytest.fail("nothing was handed off"))
        assert not result.success and "Unit already exists" in result.error


class TestWaiting:
    def wait(self, monkeypatch, answers, deadline=float("inf")):
        replies = iter(answers)
        monkeypatch.setattr(updater, "connect", lambda *a, **kw: FakeSSH())
        monkeypatch.setattr(updater, "resolve_ssh_key", lambda s: "key")
        monkeypatch.setattr(updater, "run", lambda *a, **kw: (next(replies), "", 0))
        monkeypatch.setattr(updater.time, "sleep", lambda s: None)
        return updater.wait_for_self_update(fleet()["servers"][0], "u", deadline, note="restarted")

    def test_pending_then_clean_is_a_success(self, monkeypatch):
        result = self.wait(monkeypatch, ["pending\n", "pending\n", "done 0\nSetting up docker-ce\n"])
        assert result.success and result.note == "restarted"

    def test_a_failed_update_shows_its_log_and_where_the_rest_is(self, monkeypatch):
        result = self.wait(monkeypatch, ["done 100\nE: Sub-process /usr/bin/dpkg returned an error code (1)\n"])
        assert not result.success
        assert "exit 100" in result.error and "dpkg returned" in result.error
        assert updater.SELF_LOG in result.error

    def test_an_update_that_vanished_is_not_waited_for_forever(self, monkeypatch):
        """No status file and no unit: the host restarted or the unit was stopped by hand."""
        result = self.wait(monkeypatch, ["gone\n"])
        assert not result.success and "never reported back" in result.error

    def test_the_deadline_ends_the_wait_without_touching_the_update(self, monkeypatch):
        result = self.wait(monkeypatch, ["pending\n"], deadline=0)
        assert not result.success and "still running" in result.error


class TestFinishingAfterARestart:
    def test_the_finished_hosts_survive_and_the_own_host_is_added(self, monkeypatch):
        monkeypatch.setattr(jobs.fleet_status, "invalidate", lambda: None)
        monkeypatch.setattr(jobs, "wait_for_self_update",
                            lambda server, unit, deadline, note="":
                            UpdateResult(server=server["name"], success=True, note=note))
        handoff = {"results": [UpdateResult(server="web-01", success=True, was_running=False).to_dict()],
                   "host": "docker-01", "unit": "u", "deadline": 0}
        outcome = jobs.finish_handed_off_update(fleet(), handoff)
        assert outcome.summary == "2 updated, 0 failed, 0 skipped"
        assert "web-01 (woken, updated, shut down again)" in outcome.report
        assert "docker-01 (timar restarted during it)" in outcome.report
