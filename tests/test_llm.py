"""Request construction and response parsing for all three providers.

These are the parts that silently disagree between vendors, so they are pure functions and are
tested without a network. `complete()` is a thin wrapper around them plus one httpx call.
"""
import pytest

from timar.llm import (ANTHROPIC_API_VERSION, LLMConfig, LLMError, build_models_request,
                       build_request, extract_text, parse_models)


def cfg(provider, **kw):
    defaults = {"anthropic": "claude-opus-5", "openai": "gpt-x", "ollama": "llama-x"}
    kw.setdefault("model", defaults[provider])
    kw.setdefault("base_url", LLMConfig(provider=provider).base_url or _default_base(provider))
    return LLMConfig(provider=provider, **kw)


def _default_base(provider):
    from timar.llm import DEFAULTS
    return DEFAULTS[provider]["base_url"]


class TestAnthropicRequest:
    def test_version_header_is_sent(self):
        """`anthropic-version` is part of the request contract, not an SDK convenience."""
        _, headers, _ = build_request(cfg("anthropic"), "sys", "hi")
        assert headers["anthropic-version"] == ANTHROPIC_API_VERSION

    def test_key_goes_in_x_api_key_not_authorization(self):
        _, headers, _ = build_request(cfg("anthropic", api_key="k"), "sys", "hi")
        assert headers["x-api-key"] == "k"
        assert "Authorization" not in headers

    def test_system_is_top_level_not_a_message(self):
        # Anthropic takes `system` as its own field; putting it in `messages` is an error.
        _, _, body = build_request(cfg("anthropic"), "sys", "hi")
        assert body["system"] == "sys"
        assert [m["role"] for m in body["messages"]] == ["user"]

    def test_no_sampling_parameters_are_sent(self):
        """`temperature` / `top_p` / `top_k` are rejected with a 400 on current Claude models.

        A provider-agnostic client that sends one uniform body to every backend fails here, so
        this is asserted rather than left to convention.
        """
        _, _, body = build_request(cfg("anthropic"), "sys", "hi")
        assert not {"temperature", "top_p", "top_k"} & set(body)


class TestOpenAIRequest:
    def test_bearer_token_when_key_present(self):
        _, headers, _ = build_request(cfg("openai", api_key="k"), "sys", "hi")
        assert headers["Authorization"] == "Bearer k"

    def test_no_auth_header_when_key_absent(self):
        """Local OpenAI-compatible servers (LM Studio, vLLM, llama.cpp) take no key.

        Sending `Authorization: Bearer ` is rejected outright by some of them.
        """
        _, headers, _ = build_request(cfg("openai"), "sys", "hi")
        assert "Authorization" not in headers

    def test_system_is_the_first_message(self):
        _, _, body = build_request(cfg("openai"), "sys", "hi")
        assert [m["role"] for m in body["messages"]] == ["system", "user"]


class TestOllamaRequest:
    def test_streaming_is_off(self):
        """Ollama streams by default; a streamed body does not parse as one JSON object."""
        _, _, body = build_request(cfg("ollama"), "sys", "hi")
        assert body["stream"] is False

    def test_native_chat_endpoint(self):
        url, _, _ = build_request(cfg("ollama", base_url="http://box:11434"), "sys", "hi")
        assert url == "http://box:11434/api/chat"


class TestExtractText:
    def test_anthropic_skips_leading_thinking_block(self):
        """Thinking is on by default on current Claude models, so content[0] is not the answer."""
        data = {"content": [
            {"type": "thinking", "thinking": ""},
            {"type": "text", "text": "  the answer  "},
        ]}
        assert extract_text(cfg("anthropic"), data) == "the answer"

    def test_anthropic_no_text_block_returns_empty(self):
        assert extract_text(cfg("anthropic"), {"content": [{"type": "thinking"}]}) == ""

    def test_openai(self):
        data = {"choices": [{"message": {"content": "answer"}}]}
        assert extract_text(cfg("openai"), data) == "answer"

    def test_ollama(self):
        assert extract_text(cfg("ollama"), {"message": {"content": "answer"}}) == "answer"

    @pytest.mark.parametrize("provider,data", [
        ("openai", {"choices": []}),
        ("openai", {}),
        ("ollama", {}),
    ])
    def test_malformed_response_raises_llm_error(self, provider, data):
        # Not a KeyError/IndexError escaping into the caller — callers catch LLMError only.
        with pytest.raises(LLMError):
            extract_text(cfg(provider), data)


class TestConfig:
    def test_absent_block_means_no_model_configured(self):
        """A fleet with no LLM configured is a supported state, not a broken one."""
        assert LLMConfig.from_dict(None) is None
        assert LLMConfig.from_dict({}) is None

    def test_unknown_provider_is_rejected_at_load(self):
        with pytest.raises(LLMError):
            LLMConfig.from_dict({"provider": "gemini"})

    def test_defaults_fill_in_per_provider(self):
        loaded = LLMConfig.from_dict({"provider": "ollama"})
        assert loaded.base_url == "http://localhost:11434"

    def test_trailing_slash_is_stripped(self):
        # Otherwise every URL gains a double slash, which some servers 404 on.
        loaded = LLMConfig.from_dict({"provider": "openai", "base_url": "http://x/v1/"})
        assert loaded.base_url == "http://x/v1"

    def test_missing_model_fails_at_request_time_with_a_clear_message(self):
        with pytest.raises(LLMError, match="no model configured"):
            build_request(LLMConfig(provider="openai", base_url="http://x"), "s", "p")


class TestModelsRequest:
    """Listing must work before a model is chosen — that is the whole point of it."""

    def test_no_model_is_needed_to_ask_what_the_models_are(self):
        for provider in ("anthropic", "openai", "ollama"):
            url, _ = build_models_request(LLMConfig(provider=provider, base_url=_default_base(provider)))
            assert url

    def test_anthropic_sends_the_version_header_and_the_key(self):
        url, headers = build_models_request(cfg("anthropic", api_key="k"))
        assert url.startswith("https://api.anthropic.com/v1/models")
        assert headers["anthropic-version"] == ANTHROPIC_API_VERSION
        assert headers["x-api-key"] == "k"
        assert "Authorization" not in headers

    def test_openai_uses_bearer_and_the_base_url_already_carries_v1(self):
        url, headers = build_models_request(cfg("openai", api_key="k"))
        assert url == "https://api.openai.com/v1/models"
        assert headers["Authorization"] == "Bearer k"

    def test_openai_sends_no_auth_header_without_a_key(self):
        # A local OpenAI-compatible server usually wants no key at all.
        _, headers = build_models_request(cfg("openai", api_key=""))
        assert "Authorization" not in headers

    def test_ollama_uses_its_native_tags_endpoint(self):
        url, headers = build_models_request(cfg("ollama"))
        assert url == "http://localhost:11434/api/tags"
        assert headers == {}


class TestParseModels:
    def test_anthropic_and_openai_share_the_data_id_shape(self):
        for provider in ("anthropic", "openai"):
            data = {"data": [{"id": "one"}, {"id": "two"}]}
            assert parse_models(cfg(provider), data) == ["one", "two"]

    def test_ollama_names_rather_than_ids(self):
        data = {"models": [{"name": "gpt-oss:120b-cloud"}, {"name": "glm-5.2:cloud"}]}
        assert parse_models(cfg("ollama"), data) == ["gpt-oss:120b-cloud", "glm-5.2:cloud"]

    def test_provider_order_is_preserved(self):
        """Anthropic returns newest first and Ollama most-recently-pulled first — sorting would
        bury the model the operator just installed."""
        data = {"data": [{"id": "zebra"}, {"id": "alpha"}]}
        assert parse_models(cfg("anthropic"), data) == ["zebra", "alpha"]

    def test_duplicates_and_blanks_are_dropped(self):
        data = {"data": [{"id": "one"}, {"id": "one"}, {"id": ""}, {"other": "x"}]}
        assert parse_models(cfg("openai"), data) == ["one"]

    def test_an_empty_list_is_not_an_error(self):
        # A provider with no models is a real state; the caller reports it, it is not a crash.
        assert parse_models(cfg("openai"), {"data": []}) == []
        assert parse_models(cfg("ollama"), {}) == []

    @pytest.mark.parametrize("provider,data", [
        ("anthropic", {"data": "not-a-list"}),
        ("ollama", {"models": "not-a-list"}),
    ])
    def test_a_malformed_envelope_raises_llm_error(self, provider, data):
        with pytest.raises(LLMError):
            parse_models(cfg(provider), data)
