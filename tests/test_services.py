"""Services: watched-only rows under a server — state, what is checked at draw time, the page."""
import pytest

from timar import containers, services, status as fleet_status
from tests.test_web import client, complete_setup  # noqa: F401 — the fixture

SERVERS = [
    {"name": "agent-01", "host": "10.0.0.12:22", "user": "", "ssh": False},
    {"name": "gpu-01", "host": "10.0.0.41", "user": "op"},
]


@pytest.fixture
def probes(monkeypatch):
    """agent-01 is up, gpu-01 is not; https://up.lan answers, https://down.lan does not."""
    monkeypatch.setattr(fleet_status, "is_host_up", lambda host, **kw: host.startswith("10.0.0.12"))
    monkeypatch.setattr(containers, "_healthy", lambda url, fresh=False: "up.lan" in url)
    fleet_status.invalidate()


def cfg(*entries, schemes=("agent",)):
    return {"servers": SERVERS, "link_schemes": list(schemes), "services": list(entries)}


class TestState:
    def test_health_check_decides_while_the_host_is_up(self, probes):
        out = {s.name: s for s in services.services(cfg(
            {"name": "a@rocm", "server": "agent-01", "health_url": "https://up.lan/"},
            {"name": "a@cuda", "server": "agent-01", "health_url": "https://down.lan/"},
        ))}
        assert (out["a@rocm"].state, out["a@rocm"].detail) == ("up", "running")
        assert (out["a@cuda"].state, out["a@cuda"].detail) == ("down", "the health check does not answer")
        assert out["a@rocm"].address == "up.lan"

    def test_without_a_health_check_the_host_is_the_answer(self, probes):
        (s,) = services.services(cfg({"name": "shell", "server": "agent-01"}))
        assert s.state == "up" and s.address == ""

    def test_host_not_up_is_unknown_and_its_health_is_not_asked(self, probes, monkeypatch):
        asked = []
        monkeypatch.setattr(containers, "_healthy", lambda url, fresh=False: asked.append(url) or True)
        (s,) = services.services(cfg({"name": "x", "server": "gpu-01", "health_url": "https://up.lan/"}))
        assert (s.state, s.detail) == ("unknown", "host is not up")
        assert asked == []

    def test_unknown_server_is_down(self, probes):
        (s,) = services.services(cfg({"name": "x", "server": "gone"}))
        assert (s.state, s.detail) == ("down", "host not configured")


class TestCheckedWhenDrawn:
    def test_malformed_entries_are_left_out_not_fatal(self, probes):
        out = services.services(cfg(
            "not a dict",
            {"name": "bad name!", "server": "agent-01"},
            {"name": "no-server"},
            {"name": "ok@one", "server": "agent-01"},
            {"name": "ok@one", "server": "agent-01"},     # duplicate: the first wins
        ))
        assert [s.name for s in out] == ["ok@one"]

    def test_link_must_be_http_or_a_listed_scheme(self, probes):
        out = {s.name: s.web_url for s in services.services(cfg(
            {"name": "listed", "server": "agent-01", "web_url": "agent://open"},
            {"name": "unlisted", "server": "agent-01", "web_url": "other://open"},
            {"name": "script", "server": "agent-01", "web_url": "javascript:alert(1)"},
            {"name": "page", "server": "agent-01", "web_url": "https://agent.lan"},
        ))}
        assert out == {"listed": "agent://open", "unlisted": None, "script": None,
                       "page": "https://agent.lan"}

    def test_unknown_logo_falls_back(self, probes):
        out = {s.name: s.logo for s in services.services(cfg(
            {"name": "oc", "server": "agent-01", "logo": "opencode"},
            {"name": "odd", "server": "agent-01", "logo": "../../etc"},
        ))}
        assert out == {"oc": "opencode", "odd": services.DEFAULT_LOGO}

    def test_the_opencode_logo_is_drawn(self):
        from pathlib import Path
        svg = (Path(containers.__file__).parent / "web" / "static" / "logos.svg").read_text()
        assert '<symbol id="opencode"' in svg and containers.APP_LOGOS["opencode"] == "OpenCode"


class TestRefresh:
    def test_asks_only_health_urls_whose_host_is_up(self, probes, monkeypatch):
        asked = []
        monkeypatch.setattr(containers, "_healthy", lambda url, fresh=False: asked.append((url, fresh)))
        services.refresh(cfg(
            {"name": "a", "server": "agent-01", "health_url": "https://up.lan/"},
            {"name": "b", "server": "agent-01", "health_url": "https://up.lan/"},
            {"name": "c", "server": "gpu-01", "health_url": "https://down.lan/"},
        ))
        assert asked == [("https://up.lan/", True)]


class TestPage:
    @pytest.fixture
    def page(self, client, probes):  # noqa: F811
        from timar import config
        complete_setup(client)
        config.save(cfg(
            {"name": "opencode@rocm", "server": "agent-01", "logo": "opencode",
             "web_url": "agent://open", "health_url": "https://up.lan/"},
            {"name": "opencode@cuda", "server": "agent-01", "logo": "opencode",
             "web_url": "agent://cuda", "health_url": "https://down.lan/"},
        ))
        return client

    def test_rows_sit_under_their_server(self, page):
        rows = page.get("/fragments/fleet").text
        agent = rows.index("/settings/servers/agent-01/edit")
        gpu = rows.index("/settings/servers/gpu-01/edit")
        cuda, rocm = rows.index("opencode@cuda<"), rows.index(">opencode@rocm<")
        # By name: agent-01, then its services, then gpu-01 — nothing between a host and its own.
        assert agent < cuda < gpu and agent < rocm < gpu
        assert rows.count('#opencode"') == 2

    def test_link_only_while_up_and_no_new_tab_for_a_program(self, page):
        rows = page.get("/fragments/fleet").text
        assert 'href="agent://open"' in rows
        assert 'href="agent://cuda"' not in rows       # down: the name is plain text
        link = rows.split('href="agent://open"', 1)[1].split(">", 1)[0]
        assert "_blank" not in link

    def test_a_server_rename_carries_its_services(self, page):
        from timar import config
        page.post("/settings/servers", data={
            "original_name": "agent-01", "name": "agent-02", "host": "10.0.0.12:22", "user": "",
            "platform": "linux", "no_ssh": "on"})
        assert {s["server"] for s in config.load()["services"]} == {"agent-02"}
