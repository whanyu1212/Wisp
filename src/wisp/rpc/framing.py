"""Strict bounded JSONL framing shared by external RPC transports."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from typing import cast

from pydantic import BaseModel


class RpcFrameError(ValueError):
    """One external RPC frame violated the negotiated transport contract."""


def encode_rpc_frame(model: BaseModel, *, max_frame_bytes: int) -> bytes:
    """Serialize one model as a bounded UTF-8 JSONL frame."""

    frame = model.model_dump_json().encode("utf-8")
    if len(frame) > max_frame_bytes:
        raise RpcFrameError(f"RPC frame exceeds the {max_frame_bytes}-byte limit")
    return frame + b"\n"


def decode_rpc_object(frame: bytes, *, max_frame_bytes: int) -> dict[str, object]:
    """Decode one bounded UTF-8 JSON object while rejecting duplicate keys."""

    if len(frame) > max_frame_bytes:
        raise RpcFrameError(f"RPC frame exceeds the {max_frame_bytes}-byte limit")
    try:
        text = frame.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RpcFrameError("RPC frame is not valid UTF-8") from exc

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, member in pairs:
            if key in value:
                raise RpcFrameError(f"RPC frame contains duplicate field {key!r}")
            value[key] = member
        return value

    def reject_constant(_constant: str) -> object:
        raise ValueError("non-standard numeric constant")

    def parse_finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("non-finite numeric value")
        return parsed

    try:
        value = json.loads(
            text,
            object_pairs_hook=cast(Callable[..., object], object_pairs),
            parse_constant=reject_constant,
            parse_float=parse_finite_float,
        )
    except RpcFrameError:
        raise
    except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError) as exc:
        raise RpcFrameError("RPC frame is not valid JSON") from exc
    if not isinstance(value, dict):
        raise RpcFrameError("RPC frame must be a JSON object")
    return cast(dict[str, object], value)


def pop_rpc_frame(buffer: bytearray, *, max_frame_bytes: int) -> bytes | None:
    """Pop one complete bounded frame, retaining any following bytes."""

    newline_index = buffer.find(b"\n")
    if newline_index < 0:
        awaiting_crlf_terminator = len(buffer) == max_frame_bytes + 1 and buffer.endswith(b"\r")
        if len(buffer) > max_frame_bytes and not awaiting_crlf_terminator:
            raise RpcFrameError(f"RPC frame exceeds the {max_frame_bytes}-byte limit")
        return None
    frame = bytes(buffer[:newline_index])
    if frame.endswith(b"\r"):
        frame = frame[:-1]
    if len(frame) > max_frame_bytes:
        raise RpcFrameError(f"RPC frame exceeds the {max_frame_bytes}-byte limit")
    del buffer[: newline_index + 1]
    return frame


def require_complete_rpc_stream(buffer: bytearray) -> None:
    """Reject an EOF that leaves a partial JSONL frame."""

    if buffer:
        raise RpcFrameError("RPC stream ended with an incomplete frame")


__all__ = [
    "RpcFrameError",
    "decode_rpc_object",
    "encode_rpc_frame",
    "pop_rpc_frame",
    "require_complete_rpc_stream",
]
