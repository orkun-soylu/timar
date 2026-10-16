"""Say so when a machine that should be up is not — within minutes, not in tomorrow's sweep.

The log sweep runs once a day. A host that dies an hour after it is a host nobody hears about
for twenty-three hours, and for some machines that is the whole outage: a network sensor that
sits inline, a NAS, the box that runs the backups. The status probe already asks every host
once a minute (`status`); this reads what it found.

Only **down** counts — a machine that should be up and is not (`HostStatus.state`). One that is
asleep because it is on-demand is where it should be, and alerting on it would teach the
operator to mute the bot. A host has to stay down for `down_alert_minutes` (default 5) before it
is reported, so a reboot or a missed probe is not an event; it is reported once, and once more
when it answers again, with how long it was gone.

**Undelivered messages are kept and retried every minute.** The outage that matters most can
take the internet with it — an inline sensor or the router dying cuts the LAN off from
Telegram — and an alert dropped because it could not be sent is an alert that never existed.
It arrives late, when the line is back, together with the recovery; late is still better than
never, and the times in it say when it really happened.

State is a file of its own (`alerts.json`), not `state.json`: this runs every minute next to
jobs that rewrite the state file, and two writers of one file lose each other's updates.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime

from . import config, i18n, notify
from .i18n import gettext as _

logger = logging.getLogger(__name__)

ALERTS = "alerts.json"
DEFAULT_MINUTES = 5
MAX_PENDING = 50          # a week of an unconfigured or broken bot must not grow without bound


def _load() -> dict:
    p = config.path(ALERTS)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except (ValueError, OSError) as e:
        logger.warning("alerts file unreadable, starting fresh: %s", e)
        return {}


def _save(data: dict) -> None:
    config.write_private(ALERTS, json.dumps(data, indent=2, sort_keys=True))


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def watch(cfg: dict, hosts, now: float | None = None) -> None:
    """One look at the fleet: note who went down, who came back, and send what is due."""
    minutes = cfg.get("down_alert_minutes", DEFAULT_MINUTES)
    if not minutes:                       # 0 / false: switched off
        return
    now = time.time() if now is None else now
    data = _load()
    down, pending = data.setdefault("down", {}), data.setdefault("pending", [])
    language = (cfg.get("telegram") or {}).get("language") or i18n.DEFAULT
    names = {h.name for h in hosts}

    with i18n.using(language):
        for h in hosts:
            entry = down.get(h.name)
            if h.state == "down":
                if entry is None:
                    down[h.name] = {"since": now, "told": False}
                elif not entry["told"] and now - entry["since"] >= minutes * 60:
                    entry["told"] = True
                    pending.append("🔴 " + notify.escape(_(
                        "{name} is down — no answer since {since}.",
                        name=h.name, since=_clock(entry["since"]))))
            elif entry is not None:
                del down[h.name]
                if entry["told"]:
                    pending.append("🟢 " + notify.escape(_(
                        "{name} is back — it was down from {since} for {minutes} minutes.",
                        name=h.name, since=_clock(entry["since"]),
                        minutes=round((now - entry["since"]) / 60))))
    for gone in [n for n in down if n not in names]:     # removed from the config meanwhile
        del down[gone]

    del pending[:-MAX_PENDING]
    telegram = cfg.get("telegram") or {}
    if pending and telegram.get("token"):
        try:
            notify.send(telegram["token"], telegram["chat_id"], "\n".join(pending))
            pending.clear()
        except notify.NotifyError as e:
            logger.warning("down alert not delivered, will retry: %s", e)
    _save(data)
