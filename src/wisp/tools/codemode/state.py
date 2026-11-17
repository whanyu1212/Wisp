"""Bounded JSON state, staged privately until a codemode script succeeds."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from copy import deepcopy

from pydantic import JsonValue

MAX_KEYS = 128
MAX_KEY_BYTES = 128
MAX_VALUE_BYTES = 64 * 1024
MAX_STORE_BYTES = 256 * 1024
MAX_DEPTH = 32


def _key(key: object) -> str:
    if not isinstance(key, str) or not key:
        raise ValueError("State keys must be non-empty strings")
    if len(key.encode("utf-8")) > MAX_KEY_BYTES:
        raise ValueError(f"State keys must be at most {MAX_KEY_BYTES} UTF-8 bytes")
    return key


def _json_copy(value: object, depth: int = 0) -> JsonValue:
    """Validate strict JSON and copy it at the untrusted-value boundary."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("State values must contain only finite JSON numbers")
        return value
    if isinstance(value, (dict, list)):
        if depth >= MAX_DEPTH:
            raise ValueError(f"State values must nest at most {MAX_DEPTH} containers")
        if isinstance(value, list):
            return [_json_copy(item, depth + 1) for item in value]
        copied: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("State JSON objects must have string keys")
            copied[key] = _json_copy(item, depth + 1)
        return copied
    raise ValueError(f"State values must be JSON, got {type(value).__name__}")


def _encoded(value: JsonValue) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


class ScriptState:
    """Keep JSON values across scripts, publishing successful transactions atomically.

    Args:
        initial (Mapping[str, JsonValue] | None): Initial bounded JSON state, copied
            and validated before use.
        on_commit (Callable[[dict[str, JsonValue]], None] | None): Synchronous
            persistence callback, called with a copy only when state changes. If
            it raises, the in-memory state remains unchanged.
    """

    def __init__(
        self,
        initial: Mapping[str, JsonValue] | None = None,
        on_commit: Callable[[dict[str, JsonValue]], None] | None = None,
    ) -> None:
        self._values: dict[str, JsonValue] = {}
        self._on_commit = on_commit
        self._revision = 0
        for key, value in (initial or {}).items():
            self._values[_key(key)] = _json_copy(value)
            if len(_encoded(self._values[key])) > MAX_VALUE_BYTES:
                raise ValueError(f"State values must be at most {MAX_VALUE_BYTES} JSON bytes")
        _check_store(self._values)

    def snapshot(self) -> dict[str, JsonValue]:
        """Return an independent copy of the committed state."""

        return deepcopy(self._values)

    def transaction(self) -> ScriptStateTransaction:
        """Create isolated staged state for one script; the caller must close it.

        Concurrent readers may finish freely. A changed transaction fails its
        commit if another transaction has published since it began.
        """

        return ScriptStateTransaction(self)


def _check_store(values: dict[str, JsonValue]) -> None:
    if len(values) > MAX_KEYS:
        raise ValueError(f"State must contain at most {MAX_KEYS} keys")
    if len(_encoded(values)) > MAX_STORE_BYTES:
        raise ValueError(f"State must be at most {MAX_STORE_BYTES} JSON bytes")


class ScriptStateTransaction:
    """A script's private writes; finish once with ``commit`` or ``close``.

    Every helper rejects calls after either finish operation. Helpers do no I/O
    and contain no suspension point, so a write is complete before cancellation
    can interrupt it. Only ``commit`` publishes writes to the owning state.
    """

    def __init__(self, state: ScriptState) -> None:
        self._state = state
        self._values = state.snapshot()
        self._original = _encoded(self._values)
        self._revision = state._revision
        self._closed = False

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("The script state transaction has ended")

    async def store(self, key: str, value: object) -> None:
        """Stage a copied JSON value, rejecting a write that exceeds any quota."""

        self._require_open()
        key = _key(key)
        copied = _json_copy(value)
        if len(_encoded(copied)) > MAX_VALUE_BYTES:
            raise ValueError(f"State values must be at most {MAX_VALUE_BYTES} JSON bytes")
        candidate = {**self._values, key: copied}
        _check_store(candidate)
        self._values = candidate

    async def load(self, key: str) -> JsonValue:
        """Return a copied staged value, raising KeyError when the key is absent."""

        self._require_open()
        return deepcopy(self._values[_key(key)])

    async def delete(self, key: str) -> bool:
        """Delete a staged key, returning whether it existed."""

        self._require_open()
        key = _key(key)
        if key not in self._values:
            return False
        del self._values[key]
        return True

    def commit(self) -> None:
        """Close and publish changes, persisting before updating in-memory state.

        A no-op transaction never invokes the callback. A callback exception
        propagates and leaves committed state unchanged; the transaction stays
        closed and cannot be retried.

        Raises:
            RuntimeError: If this transaction ended already, or another writer
                committed changes after it began.
        """

        self._require_open()
        self._closed = True
        if _encoded(self._values) == self._original:
            return
        if self._revision != self._state._revision:
            raise RuntimeError("Script state changed while this script was running")
        if self._state._on_commit is not None:
            self._state._on_commit(deepcopy(self._values))
        self._state._values = self._values
        self._state._revision += 1

    def close(self) -> None:
        """Discard staged changes and refuse further helper calls; safe to repeat."""

        self._closed = True
