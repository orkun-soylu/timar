"""Operator-defined checks (`checks:`): the exit code is the verdict, and a check that cannot
answer is reported rather than taken as a pass."""
import pytest

from timar import log_checker, validate
from timar.analysis import format_findings
from timar.log_checker import CheckResult, LogResult, run_check
from timar.ssh import run_bounded


@pytest.fixture
def remote(monkeypatch):
    """Replace the SSH call with a canned (stdout, stderr, exit code)."""
    answers = {}

    def fake(ssh, cmd, timeout):
        answers["timeout"] = timeout
        return answers["reply"]
    monkeypatch.setattr(log_checker, "run_bounded", fake)

    def reply(out="", err="", rc=0):
        answers["reply"] = (out, err, rc)
        return answers
    return reply


class TestRunCheck:
    @pytest.mark.parametrize("rc, state", [(0, "ok"), (1, "warning"), (2, "critical")])
    def test_the_nagios_exit_codes(self, remote, rc, state):
        remote("backup: last run 02:00\n", rc=rc)
        r = run_check(None, {"name": "backup", "cmd": "check-backup"})
        assert (r.state, r.output) == (state, "backup: last run 02:00")

    def test_any_other_exit_code_is_unknown_and_says_which(self, remote):
        remote("command not found", rc=127)
        r = run_check(None, {"name": "x", "cmd": "nope"})
        assert r.state == "unknown" and r.output.startswith("exit 127")

    def test_a_timeout_is_unknown_not_a_pass(self, remote):
        remote("partial", rc=None)
        r = run_check(None, {"name": "nfs", "cmd": "stat /mnt/nas", "timeout": 5})
        assert r.state == "unknown" and "timed out after 5 s" in r.output

    def test_the_timeout_defaults_and_can_be_set(self, remote):
        answers = remote(rc=0)
        run_check(None, {"name": "a", "cmd": "true"})
        assert answers["timeout"] == log_checker.CHECK_TIMEOUT
        run_check(None, {"name": "a", "cmd": "true", "timeout": 7})
        assert answers["timeout"] == 7

    def test_stderr_is_used_when_stdout_is_empty(self, remote):
        remote("", "mount point gone", rc=2)
        assert run_check(None, {"name": "m", "cmd": "x"}).output == "mount point gone"

    def test_output_is_capped(self, remote):
        remote("\n".join(f"line {i}" for i in range(50)), rc=1)
        assert len(run_check(None, {"name": "m", "cmd": "x"}).output.splitlines()) == log_checker.CHECK_OUTPUT_LINES

    def test_an_entry_without_cmd_is_unknown_not_a_crash(self, remote):
        remote(rc=0)
        assert run_check(None, {"name": "broken"}).state == "unknown"


class TestFindings:
    def test_a_failing_check_is_an_issue_and_is_reported(self):
        r = LogResult(server="a", success=True, checks=[
            CheckResult("backup", "ok", "fine"),
            CheckResult("nas-mount", "critical", "sentinel missing"),
        ])
        assert r.has_issues
        out = format_findings([r])
        assert "check nas-mount: CRITICAL" in out and "sentinel missing" in out
        assert "backup" not in out          # passing checks are not listed

    def test_only_passing_checks_is_clean(self):
        r = LogResult(server="a", success=True, checks=[CheckResult("backup", "ok")])
        assert not r.has_issues and format_findings([r]) == "a: clean"

    def test_unknown_is_reported_as_unknown(self):
        out = format_findings([LogResult(server="a", success=True,
                                         checks=[CheckResult("x", "unknown", "exit 127")])])
        assert "check x: UNKNOWN" in out


class TestConfig:
    def test_the_server_form_does_not_own_checks(self):
        # Saving the form keeps every key it does not own (`_carry_hand_written`); `checks` being
        # one of the form's fields would make a form save delete them.
        assert "checks" not in validate.SERVER_FIELDS


class FakeChannel:
    def __init__(self, out=b"", err=b"", rc=0, never_ends=False):
        self.out, self.err, self.rc, self.never_ends, self.closed = out, err, rc, never_ends, False

    def recv_ready(self):
        return bool(self.out)

    def recv(self, n):
        chunk, self.out = self.out[:n], self.out[n:]
        return chunk

    def recv_stderr_ready(self):
        return bool(self.err)

    def recv_stderr(self, n):
        chunk, self.err = self.err[:n], self.err[n:]
        return chunk

    def exit_status_ready(self):
        return not self.never_ends

    def recv_exit_status(self):
        return self.rc

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, channel):
        self.channel = channel

    def exec_command(self, command):
        stream = type("S", (), {"channel": self.channel})()
        return None, stream, stream


class TestRunBounded:
    def test_collects_output_and_exit_code(self):
        out, err, rc = run_bounded(FakeClient(FakeChannel(b"ok\n", b"warn\n", 1)), "x", 5)
        assert (out, err, rc) == ("ok\n", "warn\n", 1)

    def test_gives_up_and_closes_the_channel(self):
        ch = FakeChannel(b"partial", never_ends=True)
        out, _, rc = run_bounded(FakeClient(ch), "x", 0.2)
        assert rc is None and out == "partial" and ch.closed
