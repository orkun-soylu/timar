"""Devices timar only watches and links to (`ssh: false`): no job, no shutdown, a web probe."""
import pytest

from timar import config, membership, osinfo, status, updater

AP = {"name": "ap", "host": "10.0.0.2", "user": "", "platform": "linux", "ssh": False, "logo": "tplink"}
WEB = {"name": "web-01", "host": "10.0.0.7", "user": "op", "platform": "linux"}


def test_it_is_in_no_job_and_listed_in_none(monkeypatch):
    cfg = {"servers": [AP, WEB]}
    monkeypatch.setattr(membership, "is_enrolled", lambda s: True)
    assert membership.skip_reason(cfg, "update", AP) == "watched only, no SSH"
    runs, left, unenrolled = membership.lists(cfg, "update")
    assert [s["name"] for s in runs + left + unenrolled] == ["web-01"]


def test_the_update_run_does_not_mention_it(monkeypatch):
    seen = []
    monkeypatch.setattr(updater, "find_self_host", lambda cfg: None)
    monkeypatch.setattr(updater.membership, "skip_reason", lambda cfg, job, s: None)
    monkeypatch.setattr(updater, "update_server", lambda s, *a, **kw: seen.append(s["name"]) or [])
    assert updater.run_updates({"servers": [AP, WEB]}) == [] and seen == ["web-01"]


def test_it_is_probed_on_its_web_port_not_ssh(monkeypatch):
    """Measured: an access point answers on 80 and 443 and not at all on 22."""
    asked = []
    monkeypatch.setattr(status, "is_host_up", lambda host, port=None, timeout=3.0: asked.append((host, port)) or port == 80)
    status.invalidate()
    assert status._probe(status._probe_key(AP)) is True
    assert asked == [("10.0.0.2", 443), ("10.0.0.2", 80)]
    asked.clear(); status.invalidate()
    status._probe(status._probe_key({**AP, "host": "10.0.0.2:8443"}))
    assert asked == [("10.0.0.2:8443", None)]       # its own port, alone
    asked.clear(); status.invalidate()
    status._probe(status._probe_key(WEB))
    assert asked == [("10.0.0.7", None)]             # an SSH host is asked as before


def test_its_logo_and_system_come_from_its_entry():
    assert osinfo.logo(AP, {}) == "tplink" and osinfo.system(AP, {}) == "TP-Link"
    nameless = {**AP}
    nameless.pop("logo")
    assert osinfo.logo(nameless, {}) == "linux" and osinfo.system(nameless, {}) == "—"


def test_shutdown_is_refused():
    from timar import power
    with pytest.raises(power.PowerError, match="watched only"):
        power.shutdown(AP, [AP])
