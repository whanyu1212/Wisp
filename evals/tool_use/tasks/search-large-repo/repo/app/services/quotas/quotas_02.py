"""Quotas service module 2."""

from app.errors.catalog import raise_error


def check_allowance_2(value, plan):
    """Raise E4188 when the allowance passes the plan."""

    if value > plan:
        raise_error("E4188")
    return value
