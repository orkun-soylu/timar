"""Down alerts: a machine that should be up and stays down is said once, and once when back;
what could not be delivered is kept until it can be."""
from dataclasses import dataclass

import pytest

from timar import alerts, notify


@dataclass
class Host:
    name: str
    state: str


@pytest.fixture
def sent(tmp_path, monkeypatch):
    monkeypatch.setenv("TIMAR_DATA", str(tmp_path))
    out = []
    monkeypatch.setattr(notify, "send", lambda token, chat, text: out.append(text))
    return out


CFG = {"telegram": {"token": "t", "chat_id": "c"}}


def test_down_is_said_only_after_the_grace_period_and_only_once(sent):
    alerts.watch(CFG, [Host("nas", "down")], now=1000)
    alerts.watch(CFG, [Host("nas", "down")], now=1000 + 4 * 60)
    assert sent == []
    alerts.watch(CFG, [Host("nas", "down")], now=1000 + 5 * 60)
    alerts.watch(CFG, [Host("nas", "down")], now=1000 + 30 * 60)
    assert len(sent) == 1 and "nas is down" in sent[0]


def test_back_up_says_for_how_long(sent):
    alerts.watch(CFG, [Host("nas", "down")], now=0)
    alerts.watch(CFG, [Host("nas", "down")], now=600)
    alerts.watch(CFG, [Host("nas", "up")], now=1800)
    assert "nas is back" in sent[-1] and "30 minutes" in sent[-1]


def test_a_blip_shorter_than_the_grace_period_says_nothing(sent):
    alerts.watch(CFG, [Host("nas", "down")], now=0)
    alerts.watch(CFG, [Host("nas", "up")], now=120)
    alerts.watch(CFG, [Host("nas", "up")], now=1200)
    assert sent == []


def test_asleep_is_not_down(sent):
    alerts.watch(CFG, [Host("gpu", "asleep")], now=0)
    alerts.watch(CFG, [Host("gpu", "asleep")], now=3600)
    assert sent == []


def test_undelivered_is_kept_and_sent_with_the_next_one(sent, monkeypatch):
    def offline(*a):
        raise notify.NotifyError("no route to host")
    monkeypatch.setattr(notify, "send", offline)
    alerts.watch(CFG, [Host("sensor", "down")], now=0)
    alerts.watch(CFG, [Host("sensor", "down")], now=600)          # due, but cannot be sent
    monkeypatch.setattr(notify, "send", lambda token, chat, text: sent.append(text))
    alerts.watch(CFG, [Host("sensor", "up")], now=900)            # the line is back
    assert len(sent) == 1
    assert "sensor is down" in sent[0] and "sensor is back" in sent[0]


def test_zero_minutes_switches_it_off(sent):
    cfg = {**CFG, "down_alert_minutes": 0}
    alerts.watch(cfg, [Host("nas", "down")], now=0)
    alerts.watch(cfg, [Host("nas", "down")], now=3600)
    assert sent == []


def test_a_host_removed_from_the_config_is_forgotten(sent):
    alerts.watch(CFG, [Host("old", "down")], now=0)
    alerts.watch(CFG, [], now=60)
    assert alerts._load()["down"] == {}


def test_without_telegram_nothing_breaks(sent):
    alerts.watch({}, [Host("nas", "down")], now=0)
    alerts.watch({}, [Host("nas", "down")], now=600)
    assert sent == [] and alerts._load()["pending"]
