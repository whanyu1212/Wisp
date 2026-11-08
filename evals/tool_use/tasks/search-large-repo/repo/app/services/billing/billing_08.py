"""Billing service module 8."""

from app.errors.catalog import raise_error


def check_limit_8(value, plan):
    """Raise E5630 when the limit passes the plan."""

    if value > plan:
        raise_error("E5630")
    return value
