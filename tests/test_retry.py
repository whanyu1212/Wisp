from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from wisp.retry import RetryPolicy, http_retry_decision, retry_after_seconds, retry_delay_seconds


def test_retry_delay_is_exponential_jittered_and_bounded() -> None:
    policy = RetryPolicy(max_retries=2, base_delay_seconds=0.5, max_delay_seconds=1.0)

    assert retry_delay_seconds(policy, retry_number=1, random_value=lambda: 0.0) == 0.45
    assert retry_delay_seconds(policy, retry_number=2, random_value=lambda: 1.0) == 1.0
    assert (
        retry_delay_seconds(
            policy,
            retry_number=1,
            retry_after_seconds=0.8,
            random_value=lambda: 0.5,
        )
        == 0.8
    )
    assert (
        retry_delay_seconds(
            policy,
            retry_number=1,
            retry_after_seconds=1.1,
            random_value=lambda: 0.5,
        )
        is None
    )


def test_retry_after_parses_milliseconds_and_http_dates() -> None:
    now = datetime(2026, 7, 10, tzinfo=UTC)

    assert retry_after_seconds({"retry-after-ms": "250"}, now=lambda: now) == 0.25
    assert retry_after_seconds({"retry-after": "3"}, now=lambda: now) == 3.0
    assert (
        retry_after_seconds(
            {"retry-after": "Fri, 10 Jul 2026 00:00:05 GMT"},
            now=lambda: datetime(2026, 7, 10, 0, 0, tzinfo=UTC),
        )
        == 5.0
    )
    assert retry_after_seconds({"retry-after": "invalid"}, now=lambda: now) is None


@pytest.mark.parametrize(
    ("status_code", "reason"),
    [
        (408, "timeout"),
        (409, "transient_http"),
        (425, "transient_http"),
        (429, "rate_limit"),
        (503, "server_error"),
    ],
)
def test_http_retry_decision_classifies_transient_responses(status_code: int, reason: str) -> None:
    decision = http_retry_decision(status_code=status_code, headers={"retry-after": "1"})

    assert decision is not None
    assert decision.reason == reason
    assert decision.status_code == status_code
    assert decision.retry_after_seconds == 1.0


def test_http_retry_decision_rejects_terminal_http_and_quota_errors() -> None:
    assert http_retry_decision(status_code=401) is None
    assert http_retry_decision(status_code=404) is None
    assert (
        http_retry_decision(
            status_code=429,
            error_body={"error": {"code": "insufficient_quota"}},
        )
        is None
    )


def test_retry_policy_validates_bounds() -> None:
    with pytest.raises(ValidationError, match="max_delay_seconds"):
        RetryPolicy(base_delay_seconds=2, max_delay_seconds=1)
