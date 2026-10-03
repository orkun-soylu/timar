"""Powering a machine on and off by hand.

The two kinds of machine are powered by completely different mechanisms — a magic packet for a
host, `qm` on the hypervisor for a guest — and the button on the dashboard is one button. These
tests are mostly about which mechanism is chosen, and about the failures that otherwise look
exactly like success.
"""
import threading
from contextlib import contextmanager

import pytest

from timar import power


HYPERVISOR = {"name": "pve-01", "host": "10.0.0.4", "user": "root", "platform": "proxmox",
              "wol_mac": "aa:bb:cc:dd:ee:01",
              "manages_vms": [{"server_name": "kali-01", "vm_id": 100}]}
GUEST = {"name": "kali-01", "host": "10.0.0.40", "user": "op", "platform": "linux"}
SLEEPER = {"name": "gpu-01", "host": "10.0.0.2", "user": "op", "platform": "linux",
           "wol_mac": "aa:bb:cc:dd:ee:ff"}
ALWAYS_ON = {"name": "web-01", "host": "10.0.0.1", "user": "op", "platform": "linux"}
BY_HAND = {"name": "printer", "host": "10.0.0.50", "user": "op", "platform": "linux",
           "on_demand": True}

FLEET = [HYPERVISOR, GUEST, SLEEPER, ALWAYS_ON, BY_HAND]


@pytest.fixture
def ssh(monkeypatch):
    """Records what was run where, and lets a test choose the result."""
    calls = []
    outcome = {"result": ("", "", 0), "raise_on_connect": None, "raise_on_run": None,
               "by_prefix": {}}

    @contextmanager
    def fake_connect(host, user, key, timeout=30):
        if outcome["raise_on_connect"]:
            raise outcome["raise_on_connect"]
        yield ("session", host, user)

    def fake_run(session, command, timeout=120):
        calls.append({"host": session[1], "user": session[2], "command": command,
                      "timeout": timeout})
        if outcome["raise_on_run"]:
            raise outcome["raise_on_run"]
        for prefix, result in outcome["by_prefix"].items():
            if command.startswith(prefix):
                return result
        return outcome["result"]

    monkeypatch.setattr(power, "connect", fake_connect)
    monkeypatch.setattr(power, "run", fake_run)
    monkeypatch.setattr(power, "is_host_up", lambda host, **kw: True)
    monkeypatch.setattr(power.config, "resolve_ssh_key", lambda server: "/data/ssh/id_ed25519")
    return type("SSH", (), {"calls": calls, "outcome": outcome})


class TestGuestLink:
    def test_finds_the_hypervisor_and_the_id_it_knows_the_guest_by(self):
        assert power.guest_link("kali-01", FLEET) == (HYPERVISOR, 100)

    def test_a_machine_no_one_manages_is_not_a_guest(self):
        assert power.guest_link("gpu-01", FLEET) is None


class TestWake:
    def test_a_guest_is_started_by_its_hypervisor(self, ssh, monkeypatch):
        """Not by a magic packet: a guest has no wake address and never can have one, so the
        WOL path would only ever answer 'no MAC address configured'."""
        monkeypatch.setattr(power.wol, "wake",
                            lambda *a, **kw: pytest.fail("a guest has no magic packet"))
        ssh.outcome["by_prefix"] = {"qm status": ("status: stopped\n", "", 0)}
        message = power.wake(GUEST, FLEET)
        assert [c["command"] for c in ssh.calls] == ["qm status 100", "qm start 100"]
        assert {c["host"] for c in ssh.calls} == {"10.0.0.4"}   # the hypervisor, not the guest
        assert "pve-01" in message

    def test_a_guest_already_running_is_not_started_again(self, ssh):
        """`qm start` on a running VM fails — reporting that as an error would call a machine
        that is exactly where the operator wants it broken."""
        ssh.outcome["by_prefix"] = {"qm status": ("status: running\n", "", 0)}
        assert "already running" in power.wake(GUEST, FLEET)
        assert [c["command"] for c in ssh.calls] == ["qm status 100"]


class TestWakeThroughASleepingHypervisor:
    """The guest is what the operator asked for; the hypervisor is the way there."""

    @pytest.fixture
    def asleep(self, ssh, monkeypatch):
        """The hypervisor is off until its magic packet, and comes up when it is sent."""
        state = {"up": set(), "packets": []}

        def fake_wol(server, by_name):
            state["packets"].append(server["name"])
            state["up"].add(server["host"])

        monkeypatch.setattr(power.wol, "wake", fake_wol)
        monkeypatch.setattr(power, "is_host_up", lambda host, **kw: host in state["up"])
        monkeypatch.setattr(power, "wait_for_host", lambda host, **kw: host in state["up"])
        ssh.outcome["by_prefix"] = {"qm status": ("status: stopped\n", "", 0)}
        power._waking.clear()
        power._failed.clear()
        yield state
        power._waking.clear()
        power._failed.clear()

    def test_wakes_the_hypervisor_waits_for_its_guests_then_starts_the_vm(self, ssh, asleep):
        message = power.wake(GUEST, FLEET, background=False)
        assert asleep["packets"] == ["pve-01"]
        commands = [c["command"] for c in ssh.calls]
        # Its boot-time guests first: starting during `pve-guests` races the hypervisor's own
        # starts for the VM lock.
        assert "pve-guests" in commands[0]
        assert commands[1:] == ["qm status 100", "qm start 100"]
        assert "kali-01 started on pve-01" in message

    def test_a_guest_the_hypervisor_started_on_boot_is_success(self, ssh, asleep):
        ssh.outcome["by_prefix"] = {"qm status": ("status: running\n", "", 0)}
        assert "already running" in power.wake(GUEST, FLEET, background=False)
        assert not any(c["command"].startswith("qm start") for c in ssh.calls)

    def test_runs_in_the_background_and_the_row_shows_it(self, ssh, asleep, monkeypatch):
        """Minutes long — the request must not wait for it, and the dashboard must show it."""
        release = threading.Event()
        monkeypatch.setattr(power, "_wake_through", lambda *a: release.wait(5) and "done")
        message = power.wake(GUEST, FLEET)
        assert "waking pve-01 first" in message
        assert power.waking() == {"kali-01", "pve-01"}
        # A second press does not start a second chain.
        assert "already being woken" in power.wake(GUEST, FLEET)
        release.set()
        for t in threading.enumerate():
            if t.name == "wake-kali-01":
                t.join(5)
        assert power.waking() == set()
        assert power.failures() == {}

    def test_a_failure_is_kept_for_the_row_to_show(self, ssh, asleep, monkeypatch):
        """The toast is long gone when the hypervisor fails to come up."""
        monkeypatch.setattr(power, "wait_for_host", lambda host, **kw: False)
        power.wake(GUEST, FLEET)
        for t in threading.enumerate():
            if t.name == "wake-kali-01":
                t.join(5)
        assert power.waking() == set()
        assert "pve-01 did not come up" in power.failures()["kali-01"]
        assert not any(c["command"].startswith("qm start") for c in ssh.calls)

    def test_a_hypervisor_nothing_can_wake_is_named(self, ssh, asleep):
        """An always-on hypervisor that is off is a fault, not a step on the way."""
        always_on = dict(HYPERVISOR, wol_mac=None)
        with pytest.raises(power.PowerError, match="pve-01 is off and timar cannot wake it"):
            power.wake(GUEST, [always_on, GUEST])
        assert asleep["packets"] == [] and ssh.calls == []
        assert power.waking() == set()

    def test_a_host_gets_a_magic_packet(self, monkeypatch):
        sent = {}
        monkeypatch.setattr(power.wol, "wake",
                            lambda server, by_name: sent.update(name=server["name"]))
        assert "gpu-01" in power.wake(SLEEPER, FLEET)
        assert sent == {"name": "gpu-01"}

    def test_the_relay_is_named_because_it_is_where_the_packet_came_from(self, monkeypatch):
        monkeypatch.setattr(power.wol, "wake", lambda *a: None)
        server = dict(SLEEPER, wol_relay="office-nas")
        assert "via office-nas" in power.wake(server, FLEET + [server])

    def test_a_wol_failure_arrives_as_something_the_operator_can_read(self, monkeypatch):
        monkeypatch.setattr(power.wol, "wake",
                            lambda *a: (_ for _ in ()).throw(power.wol.WolError("no MAC")))
        with pytest.raises(power.PowerError, match="no MAC"):
            power.wake(SLEEPER, FLEET)


class TestShutdown:
    def test_an_always_on_machine_is_shut_down_too(self, ssh):
        """Nothing brings it back but a person — the dashboard's confirmation says so, and the
        decision is the operator's, not Timar's."""
        power.shutdown(ALWAYS_ON, FLEET)
        assert ssh.calls[0]["host"] == "10.0.0.1"
        assert ssh.calls[0]["command"] == "sudo shutdown -h now"

    def test_a_machine_switched_on_by_hand_is_shut_down_too(self, ssh):
        power.shutdown(BY_HAND, FLEET)
        assert ssh.calls[0]["host"] == "10.0.0.50"

    def test_a_guest_goes_through_its_hypervisor(self, ssh):
        message = power.shutdown(GUEST, FLEET)
        assert ssh.calls[0]["host"] == "10.0.0.4"
        assert ssh.calls[0]["command"].startswith("qm shutdown 100 --timeout ")
        assert "pve-01" in message

    def test_a_guest_shutdown_is_never_forced(self, ssh):
        """A guest ignoring ACPI needs its operator, not the equivalent of its power cord."""
        power.shutdown(GUEST, FLEET)
        assert "forceStop" not in ssh.calls[0]["command"]
        # And it is bounded, or the request would hang until a proxy gave up on it.
        assert ssh.calls[0]["timeout"] < 120

    def test_a_host_is_shut_down_over_its_own_connection(self, ssh):
        power.shutdown(SLEEPER, FLEET)
        assert ssh.calls[0]["host"] == "10.0.0.2"
        assert ssh.calls[0]["command"] == "sudo shutdown -h now"

    def test_root_does_not_reach_for_sudo(self, ssh):
        server = dict(SLEEPER, user="root")
        power.shutdown(server, [server])
        assert ssh.calls[0]["command"] == "shutdown -h now"

    def test_the_platform_decides_the_command(self, ssh):
        """busybox has no `shutdown -h now`; the router would simply report an error."""
        router = dict(SLEEPER, name="router", platform="openwrt", user="root")
        power.shutdown(router, [router])
        assert ssh.calls[0]["command"] == "poweroff"

    def test_a_dropped_connection_after_the_command_is_success(self, ssh):
        """The link going away is what a machine shutting down looks like from here."""
        ssh.outcome["raise_on_run"] = EOFError("connection closed")
        assert "shutting down" in power.shutdown(SLEEPER, FLEET)

    def test_a_connection_that_never_opened_is_a_failure(self, ssh):
        """The same exception type, the opposite meaning: nothing was asked to shut down, and
        calling it success would leave a machine running and an operator sure it was not."""
        ssh.outcome["raise_on_connect"] = OSError("no route to host")
        with pytest.raises(power.PowerError, match="could not reach gpu-01"):
            power.shutdown(SLEEPER, FLEET)

    def test_a_refused_shutdown_is_reported_rather_than_swallowed(self, ssh):
        """An account without passwordless sudo cannot halt its own machine, and the refusal is
        invisible unless the exit code is read."""
        ssh.outcome["result"] = ("", "sudo: a terminal is required to read the password", 1)
        with pytest.raises(power.PowerError, match="a terminal is required"):
            power.shutdown(SLEEPER, FLEET)

    def test_a_silent_failure_still_says_something(self, ssh):
        ssh.outcome["result"] = ("", "", 1)
        with pytest.raises(power.PowerError, match="without printing anything"):
            power.shutdown(SLEEPER, FLEET)
