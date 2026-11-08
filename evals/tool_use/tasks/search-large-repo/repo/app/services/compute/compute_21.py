"""Compute service module 21."""

from app.errors.catalog import raise_error


def check_allowance_21(value, plan):
    """Raise E8093 when the allowance passes the plan."""

    if value > plan:
        raise_error("E8093")
    return value
