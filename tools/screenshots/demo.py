"""timar on a demo fleet for the README / timar.tools screenshots.

Nothing touches the network: probes, `docker ps`, health checks and the scheduler are replaced
in this process before the app starts. TIMAR_DATA is wiped and rewritten, so point it at a
throwaway directory:

    TIMAR_DATA=/tmp/timar-demo .venv/bin/python tools/screenshots/demo.py .

then, with Playwright installed, `python tools/screenshots/capture.py docs/images`.
"""
import json
import os
import shutil
import sys
from pathlib import Path

DATA = Path(os.environ["TIMAR_DATA"])
MARK = ".timar-demo"
if DATA.exists() and any(DATA.iterdir()) and not (DATA / MARK).exists():
    sys.exit(f"{DATA} is not empty and not a demo directory — refusing to wipe it")
if DATA.exists():
    shutil.rmtree(DATA)
(DATA / "ssh").mkdir(parents=True)
(DATA / MARK).touch()
(DATA / "reports").mkdir()

SERVERS = [
    dict(name="build-01", host="10.0.0.40", user="ops", platform="linux"),
    dict(name="gpu-01", host="10.0.0.30", user="ops", platform="linux", wol_mac="aa:bb:cc:dd:ee:30"),
    dict(name="hv-01", host="10.0.0.20", user="root", platform="proxmox", web_url="https://10.0.0.20:8006",
         wol_mac="aa:bb:cc:dd:ee:20",
         manages_vms=[{"vm_id": 101, "server_name": "vm-01"}, {"vm_id": 102, "server_name": "vm-02"}]),
    dict(name="vm-01", host="10.0.0.21", user="ops", platform="linux"),
    dict(name="vm-02", host="10.0.0.22", user="ops", platform="linux"),
    dict(name="laptop", host="10.0.0.60", user="ops", platform="linux", on_demand=True),
    dict(name="nas-01", host="10.0.0.10", user="ops", platform="linux", web_url="https://nas.lan"),
    dict(name="router", host="10.0.0.1", user="root", platform="openwrt", web_url="https://10.0.0.1"),
]
UP = {"10.0.0.20", "10.0.0.21", "10.0.0.22", "10.0.0.10", "10.0.0.1"}
OS = {
    "10.0.0.40": ("fedora", "Fedora 43"), "10.0.0.30": ("ubuntu", "Ubuntu 26.04"),
    "10.0.0.20": ("proxmox", "Proxmox VE 9.2.21"), "10.0.0.21": ("debian", "Debian 13"),
    "10.0.0.22": ("ubuntu", "Ubuntu 24.04"), "10.0.0.10": ("debian", "Debian 13"),
    "10.0.0.1": ("openwrt", "OpenWrt 25.12.5"),
}
CONTAINERS = [
    dict(name="batch-tools", server="nas-01", path="/srv/batch-tools", on_demand=True),
    dict(name="git", server="nas-01", path="/srv/git", web_url="https://git.lan"),
    dict(name="monitoring", server="nas-01", path="/srv/monitoring", web_url="https://grafana.lan"),
    dict(name="photos", server="nas-01", path="/srv/photos", web_url="https://photos.lan"),
    dict(name="timar", server="nas-01", path="/srv/timar", web_url="https://timar.lan"),
    dict(name="vault", server="nas-01", path="/srv/vault", web_url="https://vault.lan"),
]
SELF_ID = "f" * 64

config = {
    "servers": SERVERS,
    "containers": CONTAINERS,
    "schedules": {"log_sweep": {"enabled": True, "kind": "daily", "at": "07:30"},
                  "update": {"enabled": True, "kind": "weekly", "at": "04:00", "day": "sunday"}},
    "job_exclude": {"update": ["router"], "log_sweep": []},
}
import yaml  # noqa: E402
(DATA / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
(DATA / "ssh/os.json").write_text(json.dumps({a: {"logo": l, "version": v} for a, (l, v) in OS.items()}))
(DATA / "ssh/enrolled").write_text("".join(f"{s['host']}\n" for s in SERVERS if s["name"] != "laptop"))

SWEEP = """One machine needs attention; the rest are clean or asleep as expected.

- nas-01: /srv is at 91% and climbed four points since last week; at this rate it fills in about two weeks. Clear old backups or grow the volume before then.
- nas-01: the monitoring project's grafana container fails its healthcheck; prometheus is up, so dashboards are blank but data is still being collected.

build-01 did not answer — it should be on. gpu-01 and the laptop were asleep and were not checked.

nas-01
  disk   /srv  91%  (threshold 85%)
  containers  unhealthy: grafana
  journal  2 errors in 25 h
    smartd: Device /dev/sda, 8 Currently unreadable (pending) sectors

router     clean
hv-01      clean
vm-01      clean
vm-02      clean
build-01   unreachable — connection timed out
gpu-01     asleep — not checked
laptop     asleep — not checked"""
UPDATE = """✅ nas-01
✅ photos (nas-01)
✅ git (nas-01)
✅ vault (nas-01)
✅ monitoring (nas-01)
✅ batch-tools (nas-01) (pulled, left stopped)
- timar (nas-01): skipped (timar itself — updated by its own release, not from inside)
✅ hv-01 (woken, updated, shut down again)
✅ vm-01 (woken, updated, shut down again)
✅ vm-02 (woken, updated, shut down again)
✅ gpu-01 (woken, updated, shut down again)
❌ build-01: Host did not come up after WOL
- router: skipped (left out of this job)
- laptop: skipped (not enrolled — enrol it from its server card)"""
RUNS = [
    ("log_sweep", "Log sweep", "2026-10-24T07:31:00", "1 with findings, 1 unreachable, 2 asleep", SWEEP),
    ("log_sweep", "Log sweep", "2026-10-22T07:31:00", "1 with findings, 2 asleep", "nas-01\n  disk   /srv  89%  (threshold 85%)"),
    ("log_sweep", "Log sweep", "2026-10-21T07:30:40", "0 with findings, 0 unreachable, 3 asleep", "All clear."),
    ("update", "Update run", "2026-10-18T04:12:00", "10 updated, 1 failed, 3 skipped", UPDATE),
]
for job, title, when, summary, report in RUNS:
    stem = when.replace("-", "").replace(":", "").replace("T", "-") + ".000000-" + job
    (DATA / "reports" / f"{stem}.json").write_text(json.dumps(dict(
        job=job, title=title, finished_at=when, ok=True, summary=summary, error="", report=report)))
from datetime import datetime as _dt
_beat = _dt.now().isoformat(timespec="seconds")
(DATA / "state.json").write_text(json.dumps({"heartbeat": {"job:log_sweep": _beat, "job:update": _beat}, "jobs": {
    "log_sweep": dict(status="ok", started_at="2026-10-24T07:30:00", last_run="2026-10-24T07:31:00",
                      last_summary=RUNS[0][3], last_report=SWEEP, last_error="", next_run="2026-10-25T07:30:00"),
    "update": dict(status="ok", started_at="2026-10-18T04:00:00", last_run="2026-10-18T04:12:00",
                   last_summary=RUNS[3][3], last_report=UPDATE, last_error="", next_run="2026-10-25T04:00:00"),
}}))

sys.path.insert(0, sys.argv[1])
from timar import containers, status  # noqa: E402
from timar.scheduler import scheduler  # noqa: E402
import timar.web.app as web  # noqa: E402

status._probe = lambda key, fresh=False: key.rsplit(":", 1)[-1] in UP
scheduler.start = lambda: None


async def _no_refresh():
    return None
web._keep_status_fresh = _no_refresh


def ps(working_dir, name, image, state="running", status_="Up 3 days", health="none", ports="", cid=None):
    return json.dumps({"ID": cid or name.ljust(64, "0"), "Names": name, "Image": image, "State": state,
                       "Status": status_, "HealthStatus": health, "Ports": ports,
                       "Labels": f"com.docker.compose.project.working_dir={working_dir}"})


PS = "\n".join([
    ps("/srv/batch-tools", "batch-tools", "batch-tools:local", state="exited", status_="Exited (0) 2 days ago"),
    ps("/srv/git", "forgejo", "codeberg.org/forgejo/forgejo:11", ports="0.0.0.0:3000->3000/tcp, [::]:3000->3000/tcp, 22/tcp"),
    ps("/srv/monitoring", "grafana", "grafana/grafana:latest", health="unhealthy", ports="0.0.0.0:3001->3000/tcp"),
    ps("/srv/monitoring", "prometheus", "prom/prometheus:v3.6", ports="127.0.0.1:9090->9090/tcp"),
    ps("/srv/photos", "immich-server", "ghcr.io/immich-app/immich-server:v2", ports="0.0.0.0:2283->2283/tcp, [::]:2283->2283/tcp"),
    ps("/srv/photos", "immich-postgres", "postgres:14", ports="5432/tcp"),
    ps("/srv/photos", "immich-valkey", "valkey/valkey:8", ports="6379/tcp"),
    ps("/srv/timar", "timar", "ghcr.io/orkun-soylu/timar:0.2.47", cid=SELF_ID),
    ps("/srv/vault", "vaultwarden", "vaultwarden/server:latest", ports="80/tcp"),
])
containers._host_containers = lambda server, fresh=False: containers.HostContainers(by_dir=containers.parse_ps(PS))
containers._healthy = lambda url, fresh=False: True
containers.self_container_id = lambda: SELF_ID

import uvicorn  # noqa: E402
uvicorn.run(web.app, host="127.0.0.1", port=8099, log_level="warning")
