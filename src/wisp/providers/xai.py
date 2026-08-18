"""xAI Responses API provider."""

from __future__ import annotations

from typing import Literal

from openai import AsyncOpenAI

from wisp.providers.auth import ProviderAuthResolver
from wisp.providers.openai import OpenAIProvider
from wisp.retry import RetryPolicy

DEFAULT_XAI_MODEL = "grok-4.6"
XAI_RESPONSES_BASE_URL = "https://api.x.ai/v1"


class XAIProvider(OpenAIProvider):
    """Provider backed by xAI's stateful Responses API."""

    name = "xai"
    _display_name = "xAI"
    _api_key_environment = "XAI_API_KEY"
    _connect_command = "/connect xai"
    _base_url = XAI_RESPONSES_BASE_URL
    # xAI documents prompt_cache_key, but Wisp should not assume its routing
    # semantics match OpenAI's until the provider-specific behavior is tested.
    supports_prompt_cache_key: Literal[False] = False

    def __init__(
        self,
        *,
        api_key: str | None = None,
        default_model: str = DEFAULT_XAI_MODEL,
        client: AsyncOpenAI | None = None,
        auth_resolver: ProviderAuthResolver | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            default_model=default_model,
            client=client,
            auth_resolver=auth_resolver,
            retry_policy=retry_policy,
        )

    def _request_options(self) -> dict[str, object]:
        # Native HTTP continuation relies on xAI's stored response chain. xAI
        # retains stored responses for 30 days; this policy is documented for
        # users rather than silently claiming stateless/ZDR continuation.
        return {"store": True}


__all__ = ["DEFAULT_XAI_MODEL", "XAIProvider", "XAI_RESPONSES_BASE_URL"]
