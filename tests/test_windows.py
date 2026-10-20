"""The Windows platform and machines updated in a window of their own.

The output fixtures are verbatim from a Windows 11 Pro (build 26300) gaming PC over its own
OpenSSH Server, PowerShell 5.1 as the login shell.
"""
from contextlib import contextmanager
from datetime import datetime

import pytest

from timar import config, membership, state, update_window, windows
from timar.platforms import PLATFORMS, Windows, get
from timar.updater import UpdateResult


@pytest.fixture(autouse=True)
def data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("TIMAR_DATA", str(tmp_path))
    membership.reset_cache()
    yield tmp_path
    membership.reset_cache()


# -- sending PowerShell ------------------------------------------------------------------------

class TestCommand:
    def test_the_script_comes_back_intact(self):
        script = "$x = 'single' + \"double\"\nif ($x) { exit 3 }"
        assert windows.decode(windows.command(script)) == script

    def test_comment_lines_and_indentation_are_not_sent(self):
        sent = windows.decode(windows.command("  # a note\n    Write-Output hi\n\n"))
        assert sent == "Write-Output hi"

    def test_nothing_a_shell_would_reinterpret_is_left_outside_the_quotes(self):
        cmd = windows.command("Write-Output \"$env:USERNAME\" | Out-String; 'a' -f 1")
        inside = cmd.split("'")[1]
        assert set(inside) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")

    def test_the_update_fits_the_command_line_limit(self):
        """CreateProcess takes 32,767 characters; 29,784 was measured to go through."""
        assert len(windows.UPDATE) < 25_000

    def test_the_update_carries_the_windows_update_worker(self):
        script = windows.decode(windows.UPDATE)
        assert "__WINDOWS_UPDATE__" not in script
        assert "CreateUpdateDownloader" in script
        # Drivers are left to their vendor's tool.
        assert "Type='Software'" in script


# -- the platform ------------------------------------------------------------------------------

class TestPlatform:
    def test_registered(self):
        assert isinstance(get("windows"), Windows)
        assert "windows" in PLATFORMS

    def test_disk(self):
        # Captured: windows.DISK on the PC — one fixed drive, 16% full.
        assert Windows().parse_disk("C 16\n", threshold=85) == []
        assert Windows().parse_disk("C 16\nD 91\n", threshold=85) == ["D: 91%"]

    def test_disk_ignores_noise(self):
        assert Windows().parse_disk("garbage\nC sixteen\n\n", threshold=0) == []

    def test_power_is_not_posix(self):
        assert Windows().shutdown_cmd("orkun") == "shutdown /s /t 0 /f"
        assert Windows().restart_cmd("orkun") == "shutdown /r /t 0 /f"

    def test_linux_is_never_restarted_or_asked_if_busy(self):
        linux = get("linux")
        assert linux.restart_cmd("op") is None
        assert linux.busy_cmd is None and linux.restart_needed_cmd is None

    def test_the_admin_key_file_is_the_one_sshd_reads(self):
        assert Windows().authorized_keys == "C:/ProgramData/ssh/administrators_authorized_keys"
        assert not Windows().password_enrol


def test_windows_is_read_as_windows():
    from timar import osinfo
    out = "windows\nlabel:Windows 11 Pro 26300\n"   # captured: windows.DETECT
    assert osinfo.parse(out) == "windows"
    assert osinfo.parse_version(out, "windows") == "Windows 11 Pro 26300"
    assert osinfo.logo({"name": "pc", "host": "10.0.0.20", "platform": "windows"}, {}) == "windows"


def test_password_enrolment_is_refused_with_the_way_to_do_it():
    from timar import enroll
    with pytest.raises(enroll.EnrollError, match="administrators_authorized_keys"):
        enroll.enroll({"name": "pc", "host": "10.0.0.20", "user": "op", "platform": "windows"},
                      "pw", grant_sudo=False)


# -- the update window -------------------------------------------------------------------------

class TestWindow:
    def test_parse(self):
        assert [t.strftime("%H:%M") for t in update_window.parse("03:00-07:00")] == ["03:00", "07:00"]
        assert update_window.parse(" 3:00 - 7:30 ")[1].minute == 30

    @pytest.mark.parametrize("bad", ["", "03:00", "3-7", "25:00-07:00", "03:00-03:00", None])
    def test_bad_specs(self, bad):
        with pytest.raises(update_window.WindowError):
            update_window.parse(bad)

    def test_open_and_closed(self):
        at = lambda h, m=0: datetime(2026, 10, 23, h, m)
        assert update_window.opening("03:00-07:00", at(3)) == at(3)
        assert update_window.opening("03:00-07:00", at(6, 59)) == at(3)
        assert update_window.opening("03:00-07:00", at(7)) is None
        assert update_window.opening("03:00-07:00", at(2, 59)) is None

    def test_across_midnight_the_opening_is_the_evening_before(self):
        assert update_window.opening("23:00-05:00", datetime(2026, 10, 23, 1)) == datetime(2026, 10, 22, 23)
        assert update_window.opening("23:00-05:00", datetime(2026, 10, 22, 23, 30)) == datetime(2026, 10, 22, 23)
        assert update_window.opening("23:00-05:00", datetime(2026, 10, 23, 12)) is None


PC = {"name": "pc", "host": "10.0.0.20", "user": "op", "platform": "windows",
      "wol_mac": "aa:bb:cc:dd:ee:ff", "update_window": "03:00-07:00"}


def _cfg(*servers):
    for s in servers:
        membership.mark_enrolled(s["host"])
    return {"servers": list(servers)}


class TestDue:
    NIGHT = datetime(2026, 10, 23, 3, 5)

    def test_due_in_its_window(self):
        assert update_window.due(_cfg(PC), self.NIGHT) == ["pc"]

    def test_not_outside_it(self):
        assert update_window.due(_cfg(PC), datetime(2026, 10, 23, 12)) == []

    def test_one_attempt_per_window(self):
        cfg = _cfg(PC)
        update_window.mark_tried("pc", PC["update_window"], self.NIGHT)
        assert update_window.due(cfg, datetime(2026, 10, 23, 5)) == []
        assert update_window.due(cfg, datetime(2026, 10, 24, 3, 1)) == ["pc"]   # next night

    def test_updated_this_week_is_not_due(self):
        cfg = _cfg(PC)
        state.set_update_window("pc", updated="2026-10-20T03:40:00")
        assert update_window.due(cfg, self.NIGHT) == []

    def test_a_week_later_it_is_due_at_the_start_not_at_last_weeks_minute(self):
        cfg = _cfg(PC)
        state.set_update_window("pc", updated="2026-10-16T03:40:00")
        assert update_window.due(cfg, datetime(2026, 10, 23, 3, 1)) == ["pc"]

    def test_its_own_interval(self):
        cfg = _cfg(PC | {"update_every_days": 1})
        state.set_update_window("pc", updated="2026-10-22T03:40:00")
        assert update_window.due(cfg, self.NIGHT) == ["pc"]

    def test_left_out_or_not_enrolled_is_never_due(self):
        cfg = _cfg(PC) | {"job_exclude": {"update": ["pc"]}}
        assert update_window.due(cfg, self.NIGHT) == []
        assert update_window.due({"servers": [PC | {"host": "10.0.0.99"}]}, self.NIGHT) == []

    def test_a_broken_spec_is_logged_not_raised(self):
        assert update_window.due(_cfg(PC | {"update_window": "soon"}), self.NIGHT) == []

    def test_only_a_successful_update_counts(self):
        cfg = _cfg(PC)
        update_window.record(cfg, [UpdateResult(server="pc", success=True, skipped=True)])
        update_window.record(cfg, [UpdateResult(server="pc", success=False)])
        assert "updated" not in state.update_windows().get("pc", {})
        update_window.record(cfg, [UpdateResult(server="pc", success=True)])
        assert state.update_windows()["pc"]["updated"]


# -- the update run on a Windows machine -------------------------------------------------------

class _Host:
    """A Windows machine. `answers` maps a command to its stdout; the update returns `update`."""

    def __init__(self, monkeypatch, *, up=True, busy="", restart="", update=("timar: Windows: 2 installed", 0)):
        from timar import updater
        self.commands = []
        self.restarted = False
        monkeypatch.setattr(updater, "is_host_up", lambda host, **kw: up)
        monkeypatch.setattr(updater, "_wake_and_wait", lambda *a, **kw: True)
        monkeypatch.setattr(updater, "wait_for_host", lambda *a, **kw: True)
        monkeypatch.setattr(updater, "_wait_offline", lambda *a, **kw: None)
        monkeypatch.setattr(updater, "_wait_for_login", lambda *a, **kw: None)
        monkeypatch.setattr(updater, "resolve_ssh_key", lambda cfg: None)
        monkeypatch.setattr(updater.osinfo, "note", lambda *a, **kw: None)

        @contextmanager
        def connect(host, user, key):
            yield host
        monkeypatch.setattr(updater, "connect", connect)

        answers = {windows.BUSY: busy, windows.RESTART_NEEDED: restart}

        def run(ssh, cmd, timeout=None):
            self.commands.append(cmd)
            if cmd == windows.UPDATE:
                return update[0], "", update[1]
            if cmd.startswith("shutdown /r"):
                self.restarted = True
            return answers.get(cmd, ""), "", 0
        monkeypatch.setattr(updater, "run", run)


class TestWindowsUpdateRun:
    def test_its_summary_line_is_the_note(self, monkeypatch):
        from timar import updater
        _Host(monkeypatch)
        [r] = updater.update_server(PC, {"pc": PC})
        assert r.success and r.note == "Windows: 2 installed"

    def test_in_use_is_skipped_and_not_updated(self, monkeypatch):
        from timar import updater
        host = _Host(monkeypatch, busy="a game is running: r5apex_dx12")
        [r] = updater.update_server(PC, {"pc": PC})
        assert r.skipped and r.success and "r5apex" in r.error
        assert windows.UPDATE not in host.commands

    def test_a_pending_restart_is_done_when_nobody_is_signed_in(self, monkeypatch):
        from timar import updater
        host = _Host(monkeypatch, restart="restart")
        [r] = updater.update_server(PC, {"pc": PC})
        assert host.restarted and r.success and "restarted" in r.note

    def test_a_signed_in_person_keeps_their_session(self, monkeypatch):
        from timar import updater
        host = _Host(monkeypatch, restart="signed-in")
        [r] = updater.update_server(PC, {"pc": PC})
        assert not host.restarted
        assert "restart pending" in r.note

    def test_a_woken_machine_is_restarted_then_shut_down(self, monkeypatch):
        from timar import updater
        host = _Host(monkeypatch, up=False, restart="restart")
        [r] = updater.update_server(PC, {"pc": PC})
        restart = host.commands.index("shutdown /r /t 0 /f")
        assert host.commands.index("shutdown /s /t 0 /f") > restart
        assert r.success and not r.was_running

    def test_a_failure_keeps_its_summary_above_the_evidence(self, monkeypatch):
        from timar import updater
        _Host(monkeypatch, update=("FAILED: KB5099999 (result 4)\ntimar: Windows: 1 installed, 1 failed", 1))
        [r] = updater.update_server(PC, {"pc": PC})
        assert not r.success
        assert r.error.startswith("Windows: 1 installed, 1 failed\n")
        assert "KB5099999" in r.error


def test_the_fleet_run_leaves_a_windowed_machine_to_its_window(monkeypatch):
    from timar import updater
    called = []
    monkeypatch.setattr(updater, "update_server", lambda s, *a, **kw: called.append(s["name"]) or [])
    monkeypatch.setattr(updater, "find_self_host", lambda cfg: None)
    web = {"name": "web", "host": "10.0.0.5", "user": "op", "platform": "linux"}
    cfg = _cfg(PC, web)
    state.set_update_window("pc", updated="2026-10-17T03:12:00")
    results = updater.run_updates(cfg)
    assert called == ["web"]
    [skipped] = [r for r in results if r.server == "pc"]
    assert skipped.skipped and "03:00-07:00" in skipped.error and "2026-10-17" in skipped.error


def test_only_runs_that_one_machine(monkeypatch):
    from timar import updater
    called = []
    monkeypatch.setattr(updater, "update_server", lambda s, *a, **kw: called.append(s["name"]) or [])
    monkeypatch.setattr(updater, "find_self_host", lambda cfg: (_ for _ in ()).throw(AssertionError))
    cfg = _cfg(PC, {"name": "web", "host": "10.0.0.5", "user": "op", "platform": "linux"})
    updater.run_updates(cfg, only="pc")
    assert called == ["pc"]


def test_the_report_keeps_both_its_note_and_that_it_was_woken():
    from timar import jobs
    [row] = jobs._update_rows([UpdateResult(server="pc", success=True, was_running=False,
                                            note="Windows: 2 installed; restarted")], False)
    assert row == "✅ pc (Windows: 2 installed; restarted; woken, updated, shut down again)"


async def test_the_window_loop_runs_a_due_machine_and_counts_the_attempt(monkeypatch):
    import asyncio
    from timar import scheduler as scheduler_module
    cfg = _cfg(PC)
    monkeypatch.setattr(scheduler_module.config, "load", lambda: cfg)
    monkeypatch.setattr(scheduler_module, "datetime", type("D", (), {"now": staticmethod(lambda: TestDue.NIGHT)}))
    calls = []
    s = scheduler_module.Scheduler()

    async def run(name, **kwargs):
        calls.append((name, kwargs))
        return len(calls) > 1      # the first is refused: the fleet's run holds the lock

    async def stop_after(n):
        if len(calls) >= 2:
            raise asyncio.CancelledError
    monkeypatch.setattr(s, "run", run)
    monkeypatch.setattr(scheduler_module.asyncio, "sleep", stop_after)
    with pytest.raises(asyncio.CancelledError):
        await s._window_loop()
    assert calls == [("update", {"only": "pc"})] * 2
    # Refused once, then run: only the run counts as this window's attempt.
    assert state.update_windows()["pc"]["tried"] == "2026-10-23T03:00:00"
    assert update_window.due(cfg, TestDue.NIGHT) == []
