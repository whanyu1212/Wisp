"""Compute service module 12."""

from app.errors.catalog import raise_error


def check_usage_12(value, plan):
    """Raise E2455 when the usage passes the plan."""

    if value > plan:
        raise_error("E2455")
    return value
