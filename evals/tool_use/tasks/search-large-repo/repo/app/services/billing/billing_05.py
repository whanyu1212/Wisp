"""Billing service module 5."""

from app.errors.catalog import raise_error


def check_allowance_5(value, plan):
    """Raise E4101 when the allowance passes the plan."""

    if value > plan:
        raise_error("E4101")
    return value
