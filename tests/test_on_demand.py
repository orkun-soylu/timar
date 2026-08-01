"""Which machines are expected to be off.

One rule, three consumers — the dashboard, the analysis prompt and the settings table — and it
was previously written out once per consumer. The third copy dropped the guest clause and said a
Kali VM was always on while the other two said it was asleep. These tests cover the rule itself,
and `test_web` covers each consumer agreeing with it.
"""
from timar.config import on_demand


def test_plain_server_is_always_on():
    assert on_demand([{"name": "web-01"}]) == {}


def test_wol_mac_marks_on_demand():
    assert on_demand([{"name": "gpu-01", "wol_mac": "aa:bb:cc:dd:ee:ff"}]) == {"gpu-01": "wol"}


def test_guest_inherits_from_an_on_demand_hypervisor():
    servers = [
        {"name": "hv-01", "platform": "proxmox", "wol_mac": "aa:bb:cc:dd:ee:ff",
         "manages_vms": [{"vm_id": 100, "server_name": "vm-01"}]},
        {"name": "vm-01"},
    ]
    assert on_demand(servers) == {"hv-01": "wol", "vm-01": "hv-01"}


def test_guest_of_an_always_on_hypervisor_stays_always_on():
    """The asymmetry is deliberate: this mistake hides a crash, the other only adds noise."""
    servers = [
        {"name": "hv-01", "platform": "proxmox",
         "manages_vms": [{"vm_id": 100, "server_name": "vm-01"}]},
        {"name": "vm-01"},
    ]
    assert on_demand(servers) == {}


def test_a_guest_with_its_own_mac_reports_the_mac():
    """Both rules apply; the machine's own wake address is the more specific answer."""
    servers = [
        {"name": "hv-01", "platform": "proxmox", "wol_mac": "aa:bb:cc:dd:ee:01",
         "manages_vms": [{"vm_id": 100, "server_name": "vm-01"}]},
        {"name": "vm-01", "wol_mac": "aa:bb:cc:dd:ee:02"},
    ]
    assert on_demand(servers)["vm-01"] == "wol"


def test_inheritance_is_transitive():
    """Nested virtualisation is unusual; answering it wrong at depth two is still wrong."""
    servers = [
        {"name": "outer", "platform": "proxmox", "wol_mac": "aa:bb:cc:dd:ee:ff",
         "manages_vms": [{"vm_id": 100, "server_name": "inner"}]},
        {"name": "inner", "platform": "proxmox",
         "manages_vms": [{"vm_id": 200, "server_name": "leaf"}]},
        {"name": "leaf"},
    ]
    assert on_demand(servers) == {"outer": "wol", "inner": "outer", "leaf": "inner"}


def test_a_cycle_terminates():
    """Hand-edited config can say anything; the fixpoint must not spin on it."""
    servers = [
        {"name": "a", "platform": "proxmox", "manages_vms": [{"vm_id": 1, "server_name": "b"}]},
        {"name": "b", "platform": "proxmox", "manages_vms": [{"vm_id": 2, "server_name": "a"}]},
    ]
    assert on_demand(servers) == {}


def test_a_guest_named_before_its_hypervisor_still_inherits():
    """Config order is the operator's; iteration order must not decide the answer."""
    servers = [
        {"name": "vm-01"},
        {"name": "hv-01", "platform": "proxmox", "wol_mac": "aa:bb:cc:dd:ee:ff",
         "manages_vms": [{"vm_id": 100, "server_name": "vm-01"}]},
    ]
    assert on_demand(servers)["vm-01"] == "hv-01"
