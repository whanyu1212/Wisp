"""Normalize portable message history without promoting tool output to instructions."""

from __future__ import annotations

import json
from collections.abc import Sequence

from .messages import Message


def active_turn_start(messages: Sequence[Message]) -> int | None:
    """Find the start of the latest user turn.

    Args:
        messages (Sequence[Message]): Ordered transcript to inspect.

    Returns:
        int | None: Index of the last user message, or None when there is none.
    """

    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "user":
            return index
    return None


def surrogate_safe_text(text: str) -> str:
    """Preserve valid Unicode while escaping malformed UTF-16 surrogates."""

    return text.encode("utf-8", errors="backslashreplace").decode("utf-8")


def _surrogate_safe_json_value(value: object) -> object:
    if isinstance(value, str):
        return surrogate_safe_text(value)
    if isinstance(value, dict):
        return {
            surrogate_safe_text(str(key)): _surrogate_safe_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_surrogate_safe_json_value(item) for item in value]
    return value


def _canonical_history_json(payload: object) -> str:
    return json.dumps(
        _surrogate_safe_json_value(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _tool_result_payload(message: Message) -> dict[str, object]:
    return {
        "call_id": message.tool_call_id,
        "is_error": message.is_error,
        "output": message.content,
        "tool_name": message.tool_name,
    }


def _portable_orphan_tool_result(message: Message) -> Message:
    return Message(
        role="assistant",
        content=_canonical_history_json(
            {
                "result": _tool_result_payload(message),
                "type": "wisp.orphan_tool_result",
                "version": 1,
            }
        ),
        created_at=message.created_at,
    )


def historical_tool_observation(message: Message) -> Message:
    """Encode an unpaired stored tool result without promoting it to user input.

    Args:
        message (Message): The orphaned tool result.

    Returns:
        Message: An assistant-role JSON observation with the original timestamp.

    Raises:
        ValueError: If the message is not a tool result.
    """

    if message.role != "tool":
        raise ValueError("Historical tool observations require a tool message")
    return _portable_orphan_tool_result(message)


def _portable_tool_exchange(
    assistant: Message,
    results: Sequence[Message],
    *,
    compatible: bool,
) -> Message:
    calls = [
        {
            "arguments": dict(call.arguments),
            "call_id": call.call_id,
            "name": call.name,
            "parse_error": call.parse_error,
        }
        for call in assistant.tool_calls or ()
    ]
    if compatible:
        payload: dict[str, object] = {
            "assistant_content": assistant.content,
            "calls": [
                {**call, "result": _tool_result_payload(result)}
                for call, result in zip(calls, results, strict=True)
            ],
            "type": "wisp.portable_tool_exchange",
            "version": 1,
        }
    else:
        payload = {
            "assistant_content": assistant.content,
            "calls": calls,
            "results": [_tool_result_payload(result) for result in results],
            "type": "wisp.incompatible_tool_exchange",
            "version": 1,
        }
    return Message(
        role="assistant",
        content=_canonical_history_json(payload),
        created_at=assistant.created_at,
    )


def _ordered_exchange_results(
    assistant: Message, results: Sequence[Message]
) -> tuple[Message, ...] | None:
    calls = assistant.tool_calls or ()
    call_ids = [call.call_id for call in calls]
    if (
        not calls
        or any(not call_id.strip() for call_id in call_ids)
        or any(not call.name.strip() or call.parse_error is not None for call in calls)
        or len(set(call_ids)) != len(call_ids)
        or len(results) != len(calls)
    ):
        return None

    results_by_id: dict[str, Message] = {}
    calls_by_id = {call.call_id: call for call in calls}
    for result in results:
        call_id = result.tool_call_id
        if not call_id or call_id not in calls_by_id or call_id in results_by_id:
            return None
        call = calls_by_id[call_id]
        if result.tool_name is not None and result.tool_name != call.name:
            return None
        results_by_id[call_id] = result

    return tuple(
        results_by_id[call.call_id].model_copy(
            update={"tool_name": results_by_id[call.call_id].tool_name or call.name}
        )
        for call in calls
    )


def provider_history_message(message: Message) -> Message:
    """Normalize one isolated durable row into non-instructional provider history.

    Sequence-aware callers should use ``normalize_provider_history`` so complete
    call/result exchanges can remain structured when the provider supports them.

    Args:
        message (Message): One isolated transcript row.

    Returns:
        Message: An assistant-role envelope for tool data, or the original row.
    """

    if message.role == "tool":
        return historical_tool_observation(message)
    if message.role == "assistant" and message.tool_calls:
        return _portable_tool_exchange(message, (), compatible=False)
    return message


def normalize_provider_history(
    messages: Sequence[Message],
    *,
    active_from: int | None = None,
    native_tool_history: bool = False,
) -> tuple[Message, ...]:
    """Validate and normalize complete tool exchanges for provider replay.

    A complete historical exchange remains native only when the target provider
    explicitly supports reconstruction from portable snapshots. Otherwise the
    exchange becomes one canonical assistant-role JSON envelope, so tool output
    never acquires user-instruction semantics. Complete exchanges belonging to the
    active turn remain structured regardless of historical replay support.

    Malformed batches and orphan results always use the assistant-role fallback;
    strict providers therefore never receive dangling calls or unmatched results.
    Exchange classification follows the assistant row, so ``active_from`` cannot
    split one call/result group into incompatible representations.

    Args:
        messages (Sequence[Message]): Ordered transcript containing complete or
            interrupted tool exchanges.
        active_from (int | None): First row belonging to the active turn. None
            treats every exchange as historical.
        native_tool_history (bool): Whether complete historical exchanges may
            retain structured calls and results for this provider.

    Returns:
        tuple[Message, ...]: Normalized history in transcript order, with each
        valid native exchange's results ordered to match its calls.

    Examples:
        An orphaned tool result becomes data in an assistant message:

        >>> from wisp.agent.messages import Message
        >>> orphan = Message(role="tool", content="done", tool_call_id="call-1")
        >>> history = normalize_provider_history([orphan])
        >>> history[0].role
        'assistant'
        >>> json.loads(history[0].content)["type"]
        'wisp.orphan_tool_result'
    """

    normalized: list[Message] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.role == "assistant" and message.tool_calls:
            result_end = index + 1
            while result_end < len(messages) and messages[result_end].role == "tool":
                result_end += 1
            results = tuple(messages[index + 1 : result_end])
            ordered_results = _ordered_exchange_results(message, results)
            is_active = active_from is not None and index >= active_from
            if ordered_results is not None and (is_active or native_tool_history):
                normalized.append(message)
                normalized.extend(ordered_results)
            else:
                normalized.append(
                    _portable_tool_exchange(
                        message,
                        ordered_results if ordered_results is not None else results,
                        compatible=ordered_results is not None,
                    )
                )
            index = result_end
            continue
        if message.role == "tool":
            normalized.append(_portable_orphan_tool_result(message))
        else:
            normalized.append(message)
        index += 1
    return tuple(normalized)
