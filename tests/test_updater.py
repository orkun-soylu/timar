"""How long one host is given to update itself, and what it says when the update fails.

The default used to be 300 seconds, chosen before anything real ran against it. A kernel upgrade
that rebuilds a DKMS module passes that on its own, and so does an update command that pulls a
7 GB container image — which is exactly what the first fleet this ran against does. The failure
was not a clean one either: paramiko's channel timeout does not reach the far end, so the update
carried on while the run recorded a failure nobody could act on.

The reporting half comes from the same first run: a failure was reported with 500 characters of
`docker compose` progress and no cause, because stderr was preferred and the wrapper script named
the broken service on stdout.
"""
from timar.updater import TAIL, DEFAULT_UPDATE_TIMEOUT, _timeout_for, failure_detail


def test_a_server_without_an_override_gets_the_default():
    assert _timeout_for({"name": "web-01"}) == DEFAULT_UPDATE_TIMEOUT


def test_an_override_wins():
    assert _timeout_for({"name": "gpu-01", "update_timeout": 3600}) == 3600


def test_a_value_from_yaml_is_coerced():
    """Hand-edited config is a supported entry point, and YAML happily yields a string here."""
    assert _timeout_for({"name": "gpu-01", "update_timeout": "3600"}) == 3600


def test_an_empty_override_falls_back_rather_than_meaning_zero():
    """Zero would be a timeout that fires before the command starts — never what was meant."""
    assert _timeout_for({"name": "gpu-01", "update_timeout": None}) == DEFAULT_UPDATE_TIMEOUT
    assert _timeout_for({"name": "gpu-01", "update_timeout": ""}) == DEFAULT_UPDATE_TIMEOUT


def test_the_default_outlasts_a_dkms_rebuild_plus_an_image_pull():
    """A regression guard on the number itself: the old 300 is what this change exists to undo."""
    assert DEFAULT_UPDATE_TIMEOUT >= 900


def test_the_line_naming_the_failure_survives_a_noisy_other_stream():
    """The real one: a wrapper script names the broken service on stdout while `docker compose`
    fills stderr with progress. Preferring either stream alone loses the cause."""
    detail = failure_detail(
        stdout="updating speedtest ...\ndone, but these services failed: speedtest",
        stderr="\n".join(f" {i:012x} Pull complete 0B" for i in range(200)),
    )
    assert "these services failed: speedtest" in detail
    assert "Pull complete" in detail


def test_each_stream_is_labelled_so_the_reader_knows_which_is_which():
    detail = failure_detail(stdout="out", stderr="err")
    assert detail == "stdout: out\nstderr: err"


def test_an_empty_stream_is_left_out_rather_than_shown_as_a_blank_label():
    assert failure_detail(stdout="", stderr="  E: dpkg was interrupted  ") == "stderr: E: dpkg was interrupted"
    assert failure_detail(stdout="only this", stderr=None) == "stdout: only this"


def test_a_command_that_fails_silently_says_so():
    """Otherwise the operator gets a bare red mark that reads like a bug in Timar."""
    assert failure_detail("", "") == "the command failed without printing anything"


def test_long_output_keeps_its_end_and_admits_it_was_cut():
    detail = failure_detail(stdout="x" * 5000, stderr="")
    assert detail.startswith("stdout: ...")
    assert detail.endswith("x" * 20)
    assert len(detail) < TAIL * 2
