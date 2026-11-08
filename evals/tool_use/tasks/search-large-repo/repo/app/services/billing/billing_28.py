"""Billing service module 28."""

from app.errors.catalog import raise_error


def check_allowance_28(value, quota):
    """Raise E1134 when the allowance passes the quota."""

    if value > quota:
        raise_error("E1134")
    return value
