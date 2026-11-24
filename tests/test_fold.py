"""Folded rows (`folded: true`): hidden under their machine behind one line that opens them."""
import pytest

from timar import containers, status as fleet_status
from timar.services import ServiceStatus
from timar.status import HostStatus
from timar.web.app import Row, _fold
from tests.test_web import client, complete_setup  # noqa: F401 — the fixture


def host(name, parent=None, folded=False, up=True):
    return HostStatus(name=name, host="10.0.0.1", up=up, on_demand=False, platform="linux",
                      parent=parent, folded=folded)


def svc(name, server, folded=False, state="up"):
    return ServiceStatus(name=name, server=server, state=state, detail="", folded=folded)


KEEP = "sort=name&dir=asc"


def names(rows):
    return [r.item.parent if r.kind == "fold" else r.item.name for r in rows]


class TestFold:
    def test_folded_rows_wait_behind_one_line_at_the_end_of_their_group(self):
        rows = [Row("server", host("hv")), Row("server", host("vm-a", "hv", folded=True), 1),
                Row("server", host("vm-b", "hv"), 1), Row("server", host("solo"))]
        out = _fold(rows, frozenset(), KEEP)
        assert [r.kind for r in out] == ["server", "server", "fold", "server"]
        assert names(out) == ["hv", "vm-b", "hv", "solo"]
        fold = out[2].item
        assert (fold.count, fold.open) == (1, False)
        assert fold.href == "/?open=hv&" + KEEP

    def test_open_shows_them_in_place_and_the_line_closes_it_again(self):
        rows = [Row("server", host("hv")), Row("server", host("vm-a", "hv", folded=True), 1),
                Row("server", host("vm-b", "hv"), 1)]
        out = _fold(rows, frozenset({"hv", "other"}), KEEP)
        assert names(out) == ["hv", "vm-a", "vm-b", "hv"]
        f = out[-1].item
        assert (f.parent, f.count, f.open, f.href) == ("hv", 1, True, "/?open=other&" + KEEP)

    def test_a_down_row_is_never_hidden(self):
        """"1 down" in the counts has to have a row to find."""
        rows = [Row("server", host("hv")), Row("service", svc("api", "hv", folded=True, state="down"), 1)]
        assert names(_fold(rows, frozenset(), KEEP)) == ["hv", "api"]

    def test_what_sits_under_a_folded_machine_folds_with_it(self):
        rows = [Row("server", host("hv")), Row("server", host("vm", "hv", folded=True), 1),
                Row("service", svc("agent", "vm"), 2)]
        out = _fold(rows, frozenset(), KEEP)
        assert names(out) == ["hv", "hv"] and out[-1].item.count == 2

    def test_a_top_level_machine_has_nothing_to_fold_under(self):
        rows = [Row("server", host("lonely", folded=True))]
        assert names(_fold(rows, frozenset(), KEEP)) == ["lonely"]


class TestPage:
    @pytest.fixture
    def page(self, client, monkeypatch):  # noqa: F811
        from timar import config
        monkeypatch.setattr(fleet_status, "is_host_up", lambda host, **kw: True)
        monkeypatch.setattr(containers, "_healthy", lambda url, fresh=False: True)
        fleet_status.invalidate()
        complete_setup(client)
        config.save({"servers": [{"name": "box", "host": "10.0.0.12:22", "user": "", "ssh": False}],
                     "services": [{"name": "daily", "server": "box"},
                                  {"name": "seldom", "server": "box", "folded": True}]})
        return client

    def test_closed_by_default_and_open_from_the_url(self, page):
        closed = page.get("/fragments/fleet").text
        assert "daily" in closed and "seldom" not in closed
        assert 'href="/?open=box&amp;sort=name&amp;dir=asc"' in closed
        opened = page.get("/fragments/fleet?open=box").text
        assert "seldom" in opened and 'aria-expanded="true"' in opened

    def test_the_poll_and_the_sort_links_keep_what_is_open(self, page):
        body = page.get("/?open=box").text
        assert "/fragments/fleet?sort=name&amp;dir=asc&amp;open=box" in body
        assert "&amp;open=box" in body.split("<thead>", 1)[1].split("</thead>", 1)[0]

    def test_a_state_filter_shows_everything_it_matches(self, page):
        assert "seldom" in page.get("/fragments/fleet?state=up").text

    def test_an_open_value_that_is_not_a_name_is_dropped(self, page):
        assert "script" not in page.get('/fragments/fleet?open="><script>').text.lower().replace("</script", "")
