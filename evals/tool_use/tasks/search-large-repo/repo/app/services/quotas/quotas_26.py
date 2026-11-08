"""Quotas service module 26."""

from app.errors.catalog import raise_error


def check_limit_26(value, plan):
    """Raise E7835 when the limit passes the plan."""

    if value > plan:
        raise_error("E7835")
    return value
