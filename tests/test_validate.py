"""Validation rules, exercised without the web layer.

These are the rules that must also hold for a hand-written config file, which is why they are
not in a form handler.
"""
import pytest

from timar.validate import ValidationError, llm, log_check, server, telegram

MINIMAL = {"name": "web-01", "host": "10.0.0.1", "user": "deploy", "platform": "linux"}


class TestServer:
    def test_minimal_entry(self):
        assert server(MINIMAL, set()) == MINIMAL

    def test_all_missing_fields_reported_at_once(self):
        """A form that reveals one problem at a time is a form people learn to dread."""
        with pytest.raises(ValidationError) as exc:
            server({}, set())
        assert len(exc.value.errors) == 4

    def test_duplicate_name_rejected(self):
        with pytest.raises(ValidationError, match="already exists"):
            server(MINIMAL, {"web-01"})

    def test_editing_a_server_may_keep_its_own_name(self):
        # Otherwise every edit that does not rename would fail on its own name.
        assert server(MINIMAL, {"web-01"}, original_name="web-01")["name"] == "web-01"

    def test_rename_onto_another_server_rejected(self):
        form = MINIMAL | {"name": "db-01"}
        with pytest.raises(ValidationError, match="already exists"):
            server(form, {"web-01", "db-01"}, original_name="web-01")

    @pytest.mark.parametrize("name", ["has space", "-leading", "semi;colon", "quote'd", ""])
    def test_awkward_names_rejected(self, name):
        # The name is a dict key, appears in report text and in log lines.
        with pytest.raises(ValidationError):
            server(MINIMAL | {"name": name}, set())

    def test_unknown_platform_rejected(self):
        with pytest.raises(ValidationError, match="Platform must be"):
            server(MINIMAL | {"platform": "windows"}, set())

    @pytest.mark.parametrize("mac", ["aa:bb:cc:dd:ee", "zz:bb:cc:dd:ee:ff", "aabbccddeeff"])
    def test_malformed_mac_rejected(self, mac):
        with pytest.raises(ValidationError, match="MAC"):
            server(MINIMAL | {"wol_mac": mac}, set())

    def test_mac_is_normalised(self):
        """Dashes and capitals are how people paste MACs; the file should hold one shape."""
        entry = server(MINIMAL | {"wol_mac": "AA-BB-CC-DD-EE-FF"}, set())
        assert entry["wol_mac"] == "aa:bb:cc:dd:ee:ff"

    def test_broadcast_without_mac_rejected(self):
        """Otherwise the file carries a setting that can never take effect."""
        with pytest.raises(ValidationError, match="needs a MAC"):
            server(MINIMAL | {"wol_broadcast": "10.0.0.255"}, set())

    def test_blank_optionals_are_omitted_not_stored_empty(self):
        entry = server(MINIMAL | {"update_cmd": "  ", "context": "", "wol_mac": ""}, set())
        assert set(entry) == set(MINIMAL)


class TestLogCheck:
    def test_defaults_round_trip(self):
        assert log_check({"journal_hours": "6", "disk_threshold": "85"}) == \
            {"journal_hours": 6, "disk_threshold": 85}

    @pytest.mark.parametrize("threshold", ["49", "100", "0"])
    def test_useless_thresholds_rejected(self, threshold):
        """Below 50 every machine is a finding; at 100 a full disk is reported too late."""
        with pytest.raises(ValidationError, match="Disk threshold"):
            log_check({"journal_hours": "6", "disk_threshold": threshold})

    def test_non_numeric_rejected(self):
        with pytest.raises(ValidationError):
            log_check({"journal_hours": "six", "disk_threshold": "85"})


class TestLLM:
    def test_empty_provider_clears_the_connection(self):
        assert llm({"provider": ""}, {"provider": "ollama"}) is None

    def test_anthropic_requires_a_key(self):
        with pytest.raises(ValidationError, match="API key is required"):
            llm({"provider": "anthropic"}, None)

    def test_local_provider_needs_no_key(self):
        assert llm({"provider": "ollama", "model": "m"}, None) == {"provider": "ollama", "model": "m"}

    def test_blank_key_keeps_the_stored_one(self):
        """The form can never show the stored key, so a blank box is its normal state.

        Treating blank as "delete" would wipe the credential on any unrelated edit to the form.
        """
        result = llm({"provider": "anthropic", "api_key": ""}, {"provider": "anthropic", "api_key": "old"})
        assert result["api_key"] == "old"

    def test_new_key_replaces_the_stored_one(self):
        result = llm({"provider": "anthropic", "api_key": "new"}, {"provider": "anthropic", "api_key": "old"})
        assert result["api_key"] == "new"

    def test_switching_provider_does_not_carry_the_old_key_over(self):
        """A key for one vendor is meaningless to another, and quietly wrong to keep."""
        result = llm({"provider": "ollama", "model": "m"}, {"provider": "anthropic", "api_key": "old"})
        assert "api_key" not in result


class TestTelegram:
    def test_both_blank_clears_it(self):
        assert telegram({"token": "", "chat_id": ""}, None) is None

    def test_chat_id_without_token_rejected(self):
        with pytest.raises(ValidationError, match="token is required"):
            telegram({"token": "", "chat_id": "123"}, None)

    def test_blank_token_keeps_the_stored_one(self):
        result = telegram({"token": "", "chat_id": "456"}, {"token": "t", "chat_id": "123"})
        assert result == {"token": "t", "chat_id": "456"}
