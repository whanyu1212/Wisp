"""Quotas service module 28."""

from app.errors.catalog import raise_error


def check_usage_28(value, plan):
    """Raise E3548 when the usage passes the plan."""

    if value > plan:
        raise_error("E3548")
    return value
