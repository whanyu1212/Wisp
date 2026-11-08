"""Quotas service module 24."""

from app.errors.catalog import raise_error


def check_exceeded_24(value, plan):
    """Raise E8493 when the exceeded passes the plan."""

    if value > plan:
        raise_error("E8493")
    return value
