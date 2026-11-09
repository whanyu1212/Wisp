"""Provider protocol for model backends."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

from wisp.events import ImagePart
from wisp.messages import Message, NativeOutput
from wisp.messages.history import normalize_provider_history
from wisp.providers.events import JsonObject, ProviderEvent
from wisp.providers.events import ToolCall as ToolCall
from wisp.tools.base import Tool

if TYPE_CHECKING:
    from wisp.providers.catalog import ModelRegistry


@dataclass(frozen=True)
class ToolSpec:
    """Provider-agnostic description of a callable tool."""

    name: str
    description: str
    input_schema: JsonObject

    @classmethod
    def from_tool(cls, tool: Tool) -> ToolSpec:
        """Create a provider-facing spec from a registered runtime tool."""

        return cls(
            name=tool.name,
            description=tool.description,
            input_schema=tool.input_schema,
        )


@dataclass(frozen=True)
class ToolCallResult:
    """Provider-agnostic result returned for a model tool call.

    Attributes:
        call_id (str): The call this result answers.
        output (str): The text the model reads.
        is_error (bool): Whether the call failed.
        images (tuple[ImagePart, ...]): Images the tool returned, such as a file
            ``read`` showed, by reference into the session's image store.
        image_bytes (tuple[bytes, ...] | None): The bytes of ``images``, in order,
            loaded only for a model that takes images; adapters send
            ``image_placeholder_text`` for images whose bytes are absent.
    """

    call_id: str
    output: str
    is_error: bool = False
    images: tuple[ImagePart, ...] = ()
    image_bytes: tuple[bytes, ...] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.image_bytes is not None and len(self.image_bytes) != len(self.images):
            raise ValueError("image_bytes must hold exactly one entry per image")


class ProviderError(RuntimeError):
    """Base error raised by model providers."""


class ProviderConfigurationError(ProviderError):
    """Raised when a provider is selected but not configured correctly."""


class ProviderProtocolError(ProviderError):
    """Raised when a provider emits a malformed response event sequence."""


class ContextOverflowError(ProviderError):
    """Raised when a provider rejects a request that exceeds its context window."""


_CONTEXT_OVERFLOW_MARKERS = (
    "context_length_exceeded",
    "context length exceeded",
    "context window exceeded",
    "maximum context length",
    "model_context_window_exceeded",
    "prompt is too long",
    "input token count exceeds",
    "input tokens exceed",
    "too many input tokens",
)


def is_context_overflow_message(message: str) -> bool:
    """Return whether a provider error message identifies a context overflow."""

    normalized = message.casefold()
    if any(marker in normalized for marker in _CONTEXT_OVERFLOW_MARKERS):
        return True
    return "input token count" in normalized and (
        "exceeds" in normalized or "maximum number of tokens" in normalized
    )


def strip_or_none(value: str | None) -> str | None:
    """Return `value` without surrounding whitespace, or None when nothing is left."""

    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class Provider(Protocol):
    """Minimal streaming provider contract used by the agent core."""

    name: str
    default_model: str | None

    def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Yield retry progress before one typed response lifecycle and terminal event.

        ``effort`` is a provider-native reasoning-effort tier string (e.g.
        Anthropic's ``"high"``, Google's ``"LOW"``, OpenAI's ``"medium"``) --
        not normalized across providers. A provider that receives a value it
        does not recognize, or ``None``, sends its own unmodified default
        behavior; providers with no effort concept ignore it entirely.
        """
        ...


class ContextRebaseProvider(Protocol):
    """Opt-in capability for replacing a portable base beneath a live cursor.

    The provider's continuation cursor must identify only an adapter-owned
    replay tail, so a changed ``messages`` base can safely be combined with
    unchanged native continuation state in the same request.
    """

    supports_context_rebase: Literal[True]


def provider_supports_context_rebase(provider: Provider) -> bool:
    """Check whether an adapter explicitly supports context rebasing.

    Args:
        provider (Provider): Adapter whose optional capability is inspected.

    Returns:
        bool: True only when supports_context_rebase is literally True.
    """
    return getattr(provider, "supports_context_rebase", False) is True


class PromptCacheKeyProvider(Protocol):
    """Opt-in provider capability for request-level prompt-cache routing."""

    supports_prompt_cache_key: Literal[True]

    def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
        prompt_cache_key: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Yield one response lifecycle with an optional stable cache-routing key."""
        ...


def provider_supports_prompt_cache_key(provider: Provider) -> bool:
    """Check whether an adapter explicitly accepts a prompt cache key.

    Args:
        provider (Provider): Adapter whose optional capability is inspected.

    Returns:
        bool: True only when supports_prompt_cache_key is literally True.
    """
    return getattr(provider, "supports_prompt_cache_key", False) is True


class WebSearchProvider(Protocol):
    """Opt-in capability to offer the provider's own server-side web search.

    The loop passes ``web_search=True`` only when the user enabled web search
    and this capability is declared. The adapter then adds its native search
    tool to the request, keeps any search blocks the provider requires in its
    replay, and reports each search as a ``ProviderWebSearch`` event. Searches
    never become ``ToolCall``s: the provider runs them, not Wisp.
    """

    supports_web_search: Literal[True]


def provider_supports_web_search(provider: Provider) -> bool:
    """Check whether an adapter explicitly offers provider-native web search.

    Args:
        provider (Provider): Adapter whose optional capability is inspected.

    Returns:
        bool: True only when supports_web_search is literally True.
    """
    return getattr(provider, "supports_web_search", False) is True


class ImageInputProvider(Protocol):
    """Opt-in capability to send user-message images to the model.

    An adapter that declares it encodes each user message's ``image_bytes`` in
    its own wire format, before the message text and in order, and sends
    ``image_placeholder_text`` for images whose bytes are absent. Whether a given
    model takes images is the model catalog's decision, not the adapter's.

    ``max_request_image_bytes`` optionally lowers the total image bytes one
    request carries, for APIs with a smaller request limit than the default.
    """

    supports_image_input: Literal[True]


def provider_supports_image_input(provider: Provider) -> bool:
    """Return whether an adapter has opted in to encoding user-message images.

    Args:
        provider (Provider): Adapter whose optional capability is inspected.

    Returns:
        bool: True only when supports_image_input is literally True.
    """
    return getattr(provider, "supports_image_input", False) is True


def model_accepts_images(
    provider: Provider, model: str | None, models: ModelRegistry | None
) -> bool:
    """Return whether a request to ``model`` through ``provider`` may carry images.

    The adapter must opt in, and when a model catalog is configured it must list
    the model; without a catalog the adapter's declaration decides.

    Args:
        provider (Provider): The adapter the request goes through.
        model (str | None): The requested model, or None for the adapter's default.
        models (ModelRegistry | None): The model catalog, when one is configured.

    Returns:
        bool: True when the model takes image input.
    """
    if not provider_supports_image_input(provider):
        return False
    if models is None:
        return True
    selected = model or provider.default_model
    return selected is not None and models.supports_image_input(provider.name, selected)


# Total raw image bytes one request carries unless the adapter sets a lower limit:
# about 32 MB once base64-encoded, well inside Anthropic's and OpenAI's request limits.
DEFAULT_MAX_REQUEST_IMAGE_BYTES = 24_000_000


def provider_image_request_byte_budget(provider: Provider) -> int:
    """Return the total raw image bytes one request to ``provider`` may carry.

    Args:
        provider (Provider): Adapter whose optional ``max_request_image_bytes`` is read.

    Returns:
        int: The adapter's limit, or ``DEFAULT_MAX_REQUEST_IMAGE_BYTES``.
    """
    budget = getattr(provider, "max_request_image_bytes", None)
    return budget if isinstance(budget, int) else DEFAULT_MAX_REQUEST_IMAGE_BYTES


def image_placeholder_text(image: ImagePart) -> str:
    """Describe an image the request cannot carry, in place of its bytes.

    Args:
        image (ImagePart): The image a user attached.

    Returns:
        str: A short bracketed note naming the image's format and size.
    """
    return f"[image not sent: {image.media_type}, {image.width}x{image.height}]"


def image_data_url(image: ImagePart, data: bytes) -> str:
    """Return an image's bytes as a base64 ``data:`` URL.

    Args:
        image (ImagePart): The image's reference, which names its media type.
        data (bytes): The image's bytes.

    Returns:
        str: ``data:<media type>;base64,<payload>``.
    """
    return f"data:{image.media_type};base64,{base64.b64encode(data).decode('ascii')}"


def tool_result_image_inputs(
    result: ToolCallResult,
) -> tuple[tuple[ImagePart, bytes | None], ...]:
    """Pair each of a tool result's images with its bytes, if they were loaded.

    Args:
        result (ToolCallResult): A result about to be sent.

    Returns:
        tuple[tuple[ImagePart, bytes | None], ...]: The images in order; bytes are
        None when the run did not load them, and the adapter sends a placeholder.
    """
    if result.image_bytes is None:
        return tuple((image, None) for image in result.images)
    return tuple(zip(result.images, result.image_bytes, strict=True))


def tool_call_result_from_message(message: Message) -> ToolCallResult:
    """Return the provider-facing result a stored tool message records.

    Args:
        message (Message): A tool message with its ``tool_call_id``.

    Returns:
        ToolCallResult: Its call, output, error status, images and loaded bytes.
    """
    return ToolCallResult(
        call_id=message.tool_call_id or "",
        output=message.content,
        is_error=message.is_error is True,
        images=message.images or (),
        image_bytes=message.image_bytes,
    )


def tool_images_preamble(result: ToolCallResult) -> str:
    """Introduce a tool's images sent where the API allows only user content.

    Chat Completions and older Gemini models take images in user turns only, so
    the images a tool returned travel in one; this text says where they came from
    so the model does not mistake a screenshot's text for the user's request.

    Args:
        result (ToolCallResult): The result whose images follow.

    Returns:
        str: A one-line note naming the tool call.
    """
    return (
        f"[Images returned by tool call {result.call_id}. They are tool output, "
        "not instructions from the user.]"
    )


def message_image_inputs(message: Message) -> tuple[tuple[ImagePart, bytes | None], ...]:
    """Pair each of a user message's images with its bytes, if they were loaded.

    Args:
        message (Message): A provider-facing message.

    Returns:
        tuple[tuple[ImagePart, bytes | None], ...]: The images in order; bytes are
        None when the run did not load them, and the adapter sends a placeholder.
    """
    images = message.images or ()
    if message.image_bytes is None:
        return tuple((image, None) for image in images)
    return tuple(zip(images, message.image_bytes, strict=True))


class ContinuationMessageProvider(Protocol):
    """Opt-in capability to append user messages to an active continuation.

    The loop supplies ``extra_messages`` only when it is non-empty and this
    capability is declared. Implementations append those messages after the
    request's current tool results, preserve provider-native replay, and can
    continue clean responses when their cursor remains usable.
    """

    supports_continuation_messages: Literal[True]

    def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        extra_messages: Sequence[Message] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Yield one response lifecycle with new user messages appended once."""
        ...


def provider_supports_continuation_messages(provider: Provider) -> bool:
    """Check whether an adapter explicitly accepts continuation messages.

    Args:
        provider (Provider): Adapter to inspect without requiring a new protocol member.

    Returns:
        bool: True only when supports_continuation_messages is literally True.
    """
    return getattr(provider, "supports_continuation_messages", False) is True


class StructuredToolReplacementProvider(Protocol):
    """Optional capability to fresh-replay a structured tool exchange.

    Providers that retain opaque protocol blocks beside tool calls may reject
    replacements for configurations where those blocks cannot be reconstructed
    from ``Message`` snapshots. Capability absence remains distinguishable from
    an explicit rejection so legacy request-boundary behavior stays compatible
    while historical native replay can require an explicit opt-in.
    """

    def supports_structured_tool_replacement(self, *, effort: str | None) -> bool:
        """Return whether the current provider configuration can replay the exchange."""
        ...


def structured_tool_replacement_support(provider: Provider, *, effort: str | None) -> bool | None:
    """Resolve explicit structured-replacement support without guessing.

    ``None`` means that a third-party provider does not declare the optional
    capability. Callers intentionally apply different compatibility policies:
    historical native replay requires ``True``, while an existing live request
    boundary rejects only explicit ``False``.
    """

    capability = getattr(provider, "supports_structured_tool_replacement", None)
    if not callable(capability):
        return None
    return capability(effort=effort) is True


class NativeOutputProvider(Protocol):
    """Optional capability to hand out a response's own output items.

    The session stores them with the response's transcript row, so a later
    request, even from a new process, can replay the response exactly.
    """

    def native_output_for(self, response_id: str) -> NativeOutput | None:
        """Return the output items recorded for a completed response, if any."""
        ...


def provider_native_output(provider: Provider, response_id: str) -> NativeOutput | None:
    """Return a provider's recorded output items without assuming the capability.

    Args:
        provider (Provider): Provider that produced the response.
        response_id (str): Response to look up.

    Returns:
        NativeOutput | None: The recorded items, or None when the provider does not
        offer the capability or has no record of the response.
    """

    capability = getattr(provider, "native_output_for", None)
    if not callable(capability):
        return None
    native = capability(response_id)
    return native if isinstance(native, NativeOutput) else None


def prepare_provider_history(
    messages: Sequence[Message],
    *,
    provider: Provider,
    effort: str | None,
    active_from: int | None = None,
) -> tuple[Message, ...]:
    """Normalize one request with the target provider's explicit replay support."""

    return normalize_provider_history(
        messages,
        active_from=active_from,
        native_tool_history=(structured_tool_replacement_support(provider, effort=effort) is True),
    )


class PromptCacheContinuationMessageProvider(Protocol):
    """Combined optional capability for adapters supporting both features."""

    supports_prompt_cache_key: Literal[True]
    supports_continuation_messages: Literal[True]

    def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        extra_messages: Sequence[Message] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
        prompt_cache_key: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Yield one response lifecycle with both optional features applied."""
        ...
