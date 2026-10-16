"""How long one host is given to update itself, and what it says when the update fails.

The default used to be 300 seconds, chosen before anything real ran against it. A kernel upgrade
that rebuilds a DKMS module passes that on its own, and so does an update command that pulls a
7 GB container image — which is exactly what the first fleet this ran against does. The failure
was not a clean one either: paramiko's channel timeout does not reach the far end, so the update
carried on while the run recorded a failure nobody could act on.

The reporting half comes from the same first run: a failure was reported with 500 characters of
`docker compose` progress and no cause, because stderr was preferred and the wrapper script named
the broken service on stdout.
"""
import pytest

from timar.updater import TAIL, DEFAULT_UPDATE_TIMEOUT, _timeout_for, failure_detail


def test_a_server_without_an_override_gets_the_default():
    assert _timeout_for({"name": "web-01"}) == DEFAULT_UPDATE_TIMEOUT


def test_an_override_wins():
    assert _timeout_for({"name": "gpu-01", "update_timeout": 3600}) == 3600


def test_a_value_from_yaml_is_coerced():
    """Hand-edited config is a supported entry point, and YAML happily yields a string here."""
    assert _timeout_for({"name": "gpu-01", "update_timeout": "3600"}) == 3600


def test_an_empty_override_falls_back_rather_than_meaning_zero():
    """Zero would be a timeout that fires before the command starts — never what was meant."""
    assert _timeout_for({"name": "gpu-01", "update_timeout": None}) == DEFAULT_UPDATE_TIMEOUT
    assert _timeout_for({"name": "gpu-01", "update_timeout": ""}) == DEFAULT_UPDATE_TIMEOUT


def test_the_default_outlasts_a_dkms_rebuild_plus_an_image_pull():
    """A regression guard on the number itself: the old 300 is what this change exists to undo."""
    assert DEFAULT_UPDATE_TIMEOUT >= 900


def test_the_line_naming_the_failure_survives_a_noisy_other_stream():
    """The real one: a wrapper script names the broken service on stdout while `docker compose`
    fills stderr with progress. Preferring either stream alone loses the cause."""
    detail = failure_detail(
        stdout="updating speedtest ...\ndone, but these services failed: speedtest",
        stderr="\n".join(f" {i:012x} Pull complete 0B" for i in range(200)),
    )
    assert "these services failed: speedtest" in detail
    assert "Pull complete" in detail


def test_each_stream_is_labelled_so_the_reader_knows_which_is_which():
    detail = failure_detail(stdout="out", stderr="err")
    assert detail == "stdout: out\nstderr: err"


def test_an_empty_stream_is_left_out_rather_than_shown_as_a_blank_label():
    assert failure_detail(stdout="", stderr="  E: dpkg was interrupted  ") == "stderr: E: dpkg was interrupted"
    assert failure_detail(stdout="only this", stderr=None) == "stdout: only this"


def test_a_command_that_fails_silently_says_so():
    """Otherwise the operator gets a bare red mark that reads like a bug in Timar."""
    assert failure_detail("", "") == "the command failed without printing anything"


def test_long_output_keeps_its_end_and_admits_it_was_cut():
    detail = failure_detail(stdout="x" * 5000, stderr="")
    assert detail.startswith("stdout: ...")
    assert detail.endswith("x" * 20)
    assert len(detail) < TAIL * 2


def test_a_machine_switched_on_by_hand_is_skipped_while_off_not_failed(monkeypatch):
    """It is exactly where it was left. A magic packet would fail on the missing MAC and paint a
    weekly red mark on a machine that is fine."""
    from timar import updater

    monkeypatch.setattr(updater, "is_host_up", lambda host, **kw: False)
    monkeypatch.setattr(updater, "wake", lambda *a, **kw: pytest.fail("must not try to wake it"))
    server = {"name": "printer", "host": "10.0.0.50", "user": "op", "platform": "linux",
              "on_demand": True}
    [result] = updater.update_server(server, {"printer": server})
    assert result.success and result.skipped and not result.was_running
    assert "by hand" in result.error


def test_an_always_on_machine_that_is_off_still_fails(monkeypatch):
    """The skip is for machines marked off-on-purpose only; an outage stays an outage."""
    from timar import updater

    monkeypatch.setattr(updater, "is_host_up", lambda host, **kw: False)
    server = {"name": "web-01", "host": "10.0.0.1", "user": "op", "platform": "linux"}
    [result] = updater.update_server(server, {"web-01": server})
    assert not result.success and not result.skipped


class _Woken:
    """A machine the run wakes: refuses `refusals` logins, then lets them in. Records commands."""

    def __init__(self, monkeypatch, refusals=0, update_fails=False):
        from contextlib import contextmanager
        from timar import updater
        self.commands, self.logins, self.refusals = [], 0, refusals
        monkeypatch.setattr(updater, "is_host_up", lambda host, **kw: False)
        monkeypatch.setattr(updater, "_wake_and_wait", lambda *a, **kw: True)
        monkeypatch.setattr(updater, "wait_for_host", lambda *a, **kw: True)
        monkeypatch.setattr(updater, "_wait_offline", lambda *a, **kw: None)
        monkeypatch.setattr(updater, "resolve_ssh_key", lambda cfg: None)
        monkeypatch.setattr(updater.time, "sleep", lambda s: None)
        monkeypatch.setattr(updater.osinfo, "note", lambda *a, **kw: None)

        outer = self

        @contextmanager
        def connect(host, user, key):
            outer.logins += 1
            if outer.logins <= outer.refusals:
                raise RuntimeError("Access denied by PAM account configuration")
            yield host
        monkeypatch.setattr(updater, "connect", connect)

        def run(ssh, cmd, timeout=None):
            outer.commands.append((ssh, cmd))
            if update_fails and "apt" in cmd:
                raise RuntimeError("Authentication failed: transport shut down or saw EOF")
            return "", "", 0
        monkeypatch.setattr(updater, "run", run)


SERVER = {"name": "gpu", "host": "10.0.0.7", "user": "op", "platform": "linux",
          "wol_mac": "aa:bb:cc:dd:ee:ff", "update_cmd": "sudo apt-get upgrade"}


def test_a_woken_machine_gets_time_to_accept_logins(monkeypatch):
    """Its SSH port opens before pam_nologin lets anyone in; one refused login is not a failure."""
    from timar import updater
    woken = _Woken(monkeypatch, refusals=3)
    [result] = updater.update_server(SERVER, {"gpu": SERVER})
    assert result.success, result.error
    assert any("apt" in c for _, c in woken.commands)


def test_a_failed_update_still_puts_the_woken_machine_back_to_sleep(monkeypatch):
    """2026-10-16: the update failed, the run returned early, and the machine stayed on all day."""
    from timar import updater
    woken = _Woken(monkeypatch, update_fails=True)
    [result] = updater.update_server(SERVER, {"gpu": SERVER})
    assert not result.success
    assert any("shutdown" in c or "poweroff" in c for _, c in woken.commands)


def test_a_vm_the_run_started_is_shut_down_even_when_its_update_failed(monkeypatch):
    from timar import updater
    woken = _Woken(monkeypatch, update_fails=True)
    host = {"name": "pve", "host": "10.0.0.4", "user": "root", "platform": "linux",
            "update_cmd": "true", "manages_vms": [{"vm_id": 106, "server_name": "dev"}]}
    vm = {"name": "dev", "host": "10.0.0.42", "user": "op", "platform": "linux",
          "update_cmd": "sudo apt-get upgrade"}
    results = updater.update_server(host, {"pve": host, "dev": vm})
    assert [r.server for r in results if not r.success] == ["dev"]
    assert ("10.0.0.4", "qm shutdown 106") in woken.commands
