"""Shared validation for provider-neutral agent runtime configuration."""

from __future__ import annotations

import math


def validate_optional_non_negative_integer(value: object, *, field: str) -> None:
    """Require an optional runtime limit to be a non-negative integer."""

    if value is not None and (type(value) is not int or value < 0):
        raise ValueError(f"{field} must be a non-negative integer or None")


def validate_optional_positive_integer(value: object, *, field: str) -> None:
    """Require an optional runtime size to be a positive integer."""

    if value is not None and (type(value) is not int or value <= 0):
        raise ValueError(f"{field} must be a positive integer or None")


def validate_non_negative_integer(value: object, *, field: str) -> None:
    """Require a runtime count to be a non-negative integer."""

    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")


def validate_agent_runtime_limits(
    *,
    max_tool_iterations: object,
    context_window: object,
    context_reserve_tokens: object,
    context_pressure_threshold: object,
) -> None:
    """Validate limits shared by the pure loop and stateful harness."""

    validate_optional_non_negative_integer(max_tool_iterations, field="max_tool_iterations")
    validate_optional_positive_integer(context_window, field="context_window")
    validate_non_negative_integer(context_reserve_tokens, field="context_reserve_tokens")
    if isinstance(context_pressure_threshold, bool) or not isinstance(
        context_pressure_threshold, int | float
    ):
        raise ValueError(
            "context_pressure_threshold must be a finite number greater than 0 and at most 1"
        )
    if not 0 < context_pressure_threshold <= 1 or not math.isfinite(context_pressure_threshold):
        raise ValueError(
            "context_pressure_threshold must be a finite number greater than 0 and at most 1"
        )
