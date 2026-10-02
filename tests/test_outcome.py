"""A job's summary drawn as chips: the stored text is the record, this only colours it."""
import pytest

from timar.web.outcome import Chip, chips


@pytest.mark.parametrize("summary, expected", [
    ("9 updated, 1 failed, 3 skipped",
     [Chip("9 updated", "up"), Chip("1 failed", "down"), Chip("3 skipped", "muted")]),
    ("7 with findings, 0 unreachable, 3 asleep, 2 not in the sweep",       # zero is dropped
     [Chip("7 with findings", "warn"), Chip("3 asleep", "muted"), Chip("2 not in the sweep", "muted")]),
    ("29 updated, 1 failed, 4 skipped — stopped by the operator",
     [Chip("29 updated", "up"), Chip("1 failed", "down"), Chip("4 skipped", "muted"),
      Chip("stopped by the operator", "")]),
])
def test_counts_become_chips_in_their_meaning_s_colour(summary, expected):
    assert chips(summary) == expected


def test_a_clean_sweep_says_so_before_its_grey_counts():
    """"3 asleep" alone is true and not reassuring; nothing failed or was found is the news."""
    assert chips("0 with findings, 0 unreachable, 3 asleep") == [Chip("", "clear"), Chip("3 asleep", "muted")]


def test_a_summary_it_does_not_know_is_shown_as_it_is():
    """Never half-parsed: a new job's wording must not lose words to a stale table."""
    assert chips("2 rebooted, 1 failed") == [Chip("2 rebooted, 1 failed", "")]
    assert chips("") == [] and chips(None) == []
