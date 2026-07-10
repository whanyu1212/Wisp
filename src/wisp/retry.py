"""Bounded, provider-owned request retry helpers."""

from __future__ import annotations

import json
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

type RetryReason = Literal[
    "network",
    "timeout",
    "rate_limit",
    "server_error",
    "transient_http",
]

_JITTER_RATIO = 0.1
_TERMINAL_QUOTA_CODES = frozenset(
    {
        "insufficient_quota",
        "billing_hard_limit_reached",
        "usage_limit_reached",
        "usage_not_included",
        "gousagelimiterror",
        "freeusagelimiterror",
    }
)


class RetryPolicy(BaseModel):
    """User-controlled bounds for retrying an unopened provider request."""

    model_config = ConfigDict(frozen=True)

    max_retries: int = Field(default=2, ge=0, le=10)
    base_delay_seconds: float = Field(default=0.5, gt=0, le=300)
    max_delay_seconds: float = Field(default=30.0, gt=0, le=300)

    @model_validator(mode="after")
    def _validate_delay_bounds(self) -> RetryPolicy:
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be at least base_delay_seconds")
        return self


class RetrySettings(BaseModel):
    """Partial retry settings accepted from a user settings file."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    max_retries: int | None = Field(default=None, ge=0, le=10)
    base_delay_seconds: float | None = Field(default=None, gt=0, le=300)
    max_delay_seconds: float | None = Field(default=None, gt=0, le=300)


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """A retryable failure classification without provider-native objects."""

    reason: RetryReason
    status_code: int | None = None
    retry_after_seconds: float | None = None


def retry_delay_seconds(
    policy: RetryPolicy,
    *,
    retry_number: int,
    retry_after_seconds: float | None = None,
    random_value: Callable[[], float] = random.random,
) -> float | None:
    """Return the delay for a 1-based retry, or ``None`` when it exceeds the cap."""

    if retry_number < 1:
        raise ValueError("retry_number must be at least 1")
    if retry_after_seconds is not None and retry_after_seconds > policy.max_delay_seconds:
        return None

    jitter = 1 + ((random_value() * 2) - 1) * _JITTER_RATIO
    local_delay = min(
        policy.max_delay_seconds,
        policy.base_delay_seconds * (2 ** (retry_number - 1)) * jitter,
    )
    return float(max(local_delay, retry_after_seconds or 0.0))


def http_retry_decision(
    *,
    status_code: int,
    headers: Mapping[str, str] | None = None,
    error_body: object | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RetryDecision | None:
    """Classify an HTTP response according to Wisp's bounded retry policy."""

    if status_code == 429:
        if _contains_terminal_quota_code(error_body):
            return None
        reason: RetryReason = "rate_limit"
    elif status_code == 408:
        reason = "timeout"
    elif status_code in {409, 425}:
        reason = "transient_http"
    elif 500 <= status_code <= 599:
        reason = "server_error"
    else:
        return None

    return RetryDecision(
        reason=reason,
        status_code=status_code,
        retry_after_seconds=retry_after_seconds(headers or {}, now=now),
    )


def retry_after_seconds(
    headers: Mapping[str, str],
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> float | None:
    """Parse standard and OpenAI millisecond retry headers, when present."""

    normalized = {key.lower(): value.strip() for key, value in headers.items()}
    if retry_after_ms := normalized.get("retry-after-ms"):
        try:
            return max(0.0, float(retry_after_ms) / 1000)
        except ValueError:
            pass

    retry_after = normalized.get("retry-after")
    if not retry_after:
        return None
    try:
        return max(0.0, float(retry_after))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(retry_after)
    except (TypeError, ValueError, IndexError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max(0.0, (retry_at - now()).total_seconds())


def _contains_terminal_quota_code(error_body: object | None) -> bool:
    """Recognize stable provider quota identifiers without parsing prose errors."""

    return any(value.lower() in _TERMINAL_QUOTA_CODES for value in _error_identifiers(error_body))


def _error_identifiers(error_body: object | None) -> tuple[str, ...]:
    if error_body is None:
        return ()
    if isinstance(error_body, bytes):
        error_body = error_body.decode("utf-8", errors="replace")
    if isinstance(error_body, str):
        try:
            error_body = json.loads(error_body)
        except json.JSONDecodeError:
            return ()

    identifiers: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if key in {"code", "type"} and isinstance(nested, str):
                    identifiers.append(nested)
                collect(nested)
        elif isinstance(value, list | tuple):
            for nested in value:
                collect(nested)

    collect(error_body)
    return tuple(identifiers)


__all__ = [
    "RetryDecision",
    "RetryPolicy",
    "RetryReason",
    "RetrySettings",
    "http_retry_decision",
    "retry_after_seconds",
    "retry_delay_seconds",
]
