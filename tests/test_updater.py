"""How long one host is given to update itself.

The default used to be 300 seconds, chosen before anything real ran against it. A kernel upgrade
that rebuilds a DKMS module passes that on its own, and so does an update command that pulls a
7 GB container image — which is exactly what the first fleet this ran against does. The failure
was not a clean one either: paramiko's channel timeout does not reach the far end, so the update
carried on while the run recorded a failure nobody could act on.
"""
from timar.updater import DEFAULT_UPDATE_TIMEOUT, _timeout_for


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
