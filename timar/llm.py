"""One completion call, three providers.

Timar needs a model to summarise log sweeps. Which model is the operator's decision, and for a
self-hosted tool that decision usually is not "sign up for a cloud API" — the machines being
managed frequently sit next to one that already runs Ollama. Requiring an Anthropic or OpenAI
key to use the log analysis at all would put a paywall in front of a homelab tool.

So the provider is configuration. The rest of the codebase calls `complete()` and never learns
which one answered.

**Raw HTTP rather than each vendor's SDK.** Three SDKs to install and keep current, in a
single-container image, to make one request each — and the SDKs disagree about everything from
retry policy to how a response is shaped, which is precisely the disagreement this module exists
to hide. One `httpx` dependency covers all three, and the request shapes below are small enough
to read in full.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

ANTHROPIC = "anthropic"
OPENAI = "openai"
OLLAMA = "ollama"

PROVIDERS = (ANTHROPIC, OPENAI, OLLAMA)

# The version header is part of Anthropic's request contract, not a client library detail.
ANTHROPIC_API_VERSION = "2023-06-01"

DEFAULTS = {
    ANTHROPIC: {"base_url": "https://api.anthropic.com", "model": "claude-opus-5"},
    OPENAI: {"base_url": "https://api.openai.com/v1", "model": ""},
    # Anything speaking Ollama's native API: a local daemon, or a LAN box the fleet already has.
    OLLAMA: {"base_url": "http://localhost:11434", "model": ""},
}


class LLMError(RuntimeError):
    """The model could not be reached or did not answer in a shape we understand."""


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    max_tokens: int = 4000
    timeout: float = 120.0

    @classmethod
    def from_dict(cls, raw: dict | None) -> "LLMConfig | None":
        """Build a config from the `llm:` block, or None when the operator has not set one.

        None is a supported state, not an error: wake, update and the log sweep all work with no
        model configured. Only the written analysis is unavailable.
        """
        if not raw or not raw.get("provider"):
            return None
        provider = raw["provider"]
        if provider not in PROVIDERS:
            raise LLMError(f"unknown provider {provider!r}; expected one of {', '.join(PROVIDERS)}")
        defaults = DEFAULTS[provider]
        return cls(
            provider=provider,
            model=raw.get("model") or defaults["model"],
            api_key=raw.get("api_key", ""),
            base_url=(raw.get("base_url") or defaults["base_url"]).rstrip("/"),
            max_tokens=int(raw.get("max_tokens", 4000)),
            timeout=float(raw.get("timeout", 120.0)),
        )


def build_request(cfg: LLMConfig, system: str, prompt: str) -> tuple[str, dict, dict]:
    """(url, headers, json_body) for one completion. Pure — this is what the tests exercise."""
    if not cfg.model:
        raise LLMError(f"no model configured for provider {cfg.provider!r}")

    if cfg.provider == ANTHROPIC:
        return (
            f"{cfg.base_url}/v1/messages",
            {
                "x-api-key": cfg.api_key,
                "anthropic-version": ANTHROPIC_API_VERSION,
                "content-type": "application/json",
            },
            {
                "model": cfg.model,
                "max_tokens": cfg.max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            },
        )

    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]

    if cfg.provider == OPENAI:
        headers = {"content-type": "application/json"}
        if cfg.api_key:  # a local OpenAI-compatible server usually wants no key at all
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        return (
            f"{cfg.base_url}/chat/completions",
            headers,
            {"model": cfg.model, "max_tokens": cfg.max_tokens, "messages": messages},
        )

    return (
        f"{cfg.base_url}/api/chat",
        {"content-type": "application/json"},
        {"model": cfg.model, "messages": messages, "stream": False},
    )


def extract_text(cfg: LLMConfig, data: dict) -> str:
    """Pull the answer out of a provider's response envelope. Pure."""
    try:
        if cfg.provider == ANTHROPIC:
            # `content` is a list of blocks and the first one is not necessarily the answer:
            # thinking is on by default on current models, so a thinking block can precede the
            # text. Select by type rather than by position.
            for block in data.get("content", []):
                if block.get("type") == "text":
                    return block.get("text", "").strip()
            return ""
        if cfg.provider == OPENAI:
            return (data["choices"][0]["message"].get("content") or "").strip()
        return (data["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise LLMError(f"unexpected response shape from {cfg.provider}: {e}") from e


def complete(cfg: LLMConfig, system: str, prompt: str) -> str:
    """Run one completion. Raises LLMError; callers decide whether that is fatal."""
    url, headers, body = build_request(cfg, system, prompt)
    try:
        response = httpx.post(url, headers=headers, json=body, timeout=cfg.timeout)
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        # The body carries the actual reason (bad model name, missing credit, unknown field) and
        # the status alone sends the operator hunting. Keep it, but clipped.
        detail = e.response.text[:300].replace("\n", " ")
        raise LLMError(f"{cfg.provider} returned HTTP {e.response.status_code}: {detail}") from e
    except httpx.HTTPError as e:
        raise LLMError(f"could not reach {cfg.provider} at {cfg.base_url}: {e}") from e

    return extract_text(cfg, response.json())
