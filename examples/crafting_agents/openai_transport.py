"""Optional SDK transport; imported only by the live chapter 4 command."""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import cast

from httpx import AsyncClient, HTTPError
from openai import APIConnectionError, APIStatusError, AsyncOpenAI, AsyncStream, OpenAIError
from openai.types.responses import ResponseStreamEvent

from examples.crafting_agents.responses import EventStream, OpenFailure, Payload, ResponseFailure


class SDKEventStream:
    """Expose decoded native events while owning the acquired SDK stream."""

    def __init__(self, stream: AsyncStream[ResponseStreamEvent]) -> None:
        self.stream = stream

    def __aiter__(self) -> AsyncIterator[Payload]:
        return self

    async def __anext__(self) -> Payload:
        try:
            event = await self.stream.__anext__()
        except (OpenAIError, HTTPError) as exc:
            raise ResponseFailure("upstream stream failed; not retried") from exc
        return event.model_dump(mode="json")

    async def aclose(self) -> None:
        await self.stream.close()


class OpenAITransport:
    """Own SDK client lifetime and classify failures while opening a request."""

    def __init__(self, api_key: str, *, http_client: AsyncClient | None = None) -> None:
        # Pin the endpoint and disable SDK retries so there is only one retry owner.
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://api.openai.com/v1",
            max_retries=0,
            timeout=30.0,
            http_client=http_client,
        )

    # ANCHOR: open
    async def open(self, request: Payload) -> EventStream:
        """Acquire a stream with a conservative network/server-error retry classification.

        Args:
            request (Payload): Native payload from build_request.

        Returns:
            EventStream: Owned, decoded native event stream.

        Raises:
            OpenFailure: The request could not be opened; only network/5xx failures retry.
        """
        create = cast(
            Callable[..., Awaitable[AsyncStream[ResponseStreamEvent]]], self.client.responses.create
        )
        try:
            return SDKEventStream(await create(**request))
        except APIConnectionError as exc:
            raise OpenFailure("request connection failed", retryable=True) from exc
        except APIStatusError as exc:
            raise OpenFailure(
                f"request rejected (HTTP {exc.status_code})", retryable=500 <= exc.status_code < 600
            ) from exc
        except OpenAIError as exc:
            raise OpenFailure("request could not be opened", retryable=False) from exc

    # ANCHOR_END: open

    async def aclose(self) -> None:
        await self.client.close()
