"""DeepSeek Chat Completions provider."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionChunk

from wisp.providers.auth import ProviderAuthResolver
from wisp.providers.events import ProviderUsage, ToolCall
from wisp.providers.openai_compatible import ChatPayload, OpenAICompatibleProvider
from wisp.retry import RetryPolicy

DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"


class DeepSeekProvider(OpenAICompatibleProvider):
    """Provider for DeepSeek's thinking-capable Chat Completions API."""

    name = "deepseek"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        default_model: str = DEFAULT_DEEPSEEK_MODEL,
        client: AsyncOpenAI | None = None,
        auth_resolver: ProviderAuthResolver | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        super().__init__(
            base_url=DEEPSEEK_BASE_URL,
            default_model=default_model,
            api_key=api_key,
            client=client,
            auth_resolver=auth_resolver,
            retry_policy=retry_policy,
        )

    def supports_structured_tool_replacement(self, *, effort: str | None) -> bool:
        """Reject fresh reconstruction because thinking tool turns need native reasoning."""

        del effort
        return False

    def _request_options(self, *, effort: str | None) -> dict[str, object]:
        options: dict[str, object] = {
            "extra_body": {"thinking": {"type": "enabled"}},
        }
        if effort is not None:
            options["reasoning_effort"] = effort
        return options

    def _reasoning_delta(self, delta: object) -> str | None:
        return _extension_string(delta, "reasoning_content")

    def _assistant_replay_message(
        self,
        *,
        content: str,
        reasoning_content: str,
        tool_calls: Sequence[ToolCall],
    ) -> ChatPayload:
        message = super()._assistant_replay_message(
            content=content,
            reasoning_content=reasoning_content,
            tool_calls=tool_calls,
        )
        if reasoning_content:
            message["reasoning_content"] = reasoning_content
        return message

    def _usage_from_chat(self, chunk: ChatCompletionChunk) -> ProviderUsage | None:
        usage = chunk.usage
        if usage is None:
            return None
        cache_hits = _extension_int(usage, "prompt_cache_hit_tokens")
        reasoning_tokens = _extension_int(usage, "reasoning_tokens")
        return ProviderUsage(
            input_tokens=max(0, usage.prompt_tokens),
            output_tokens=max(0, usage.completion_tokens),
            total_tokens=max(0, usage.total_tokens),
            cache_read_input_tokens=(max(0, cache_hits) if cache_hits is not None else None),
            reasoning_output_tokens=(
                max(0, reasoning_tokens) if reasoning_tokens is not None else None
            ),
        )

    def _provider_api_key_environment(self) -> str:
        return DEEPSEEK_API_KEY_ENV

    def _fallback_api_key_environment(self) -> str | None:
        return None


def _extension_string(value: object, name: str) -> str | None:
    direct = getattr(value, name, None)
    if isinstance(direct, str):
        return direct
    extra = getattr(value, "model_extra", None)
    if isinstance(extra, Mapping):
        candidate = extra.get(name)
        if isinstance(candidate, str):
            return candidate
    return None


def _extension_int(value: object, name: str) -> int | None:
    direct = getattr(value, name, None)
    if isinstance(direct, int) and not isinstance(direct, bool):
        return direct
    extra = getattr(value, "model_extra", None)
    if isinstance(extra, Mapping):
        candidate = extra.get(name)
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return candidate
    return None


__all__ = [
    "DEEPSEEK_API_KEY_ENV",
    "DEEPSEEK_BASE_URL",
    "DEFAULT_DEEPSEEK_MODEL",
    "DeepSeekProvider",
]
