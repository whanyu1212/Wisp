"""Billing service module 6."""

from app.errors.catalog import raise_error


def check_allowance_6(value, quota):
    """Raise E5080 when the allowance passes the quota."""

    if value > quota:
        raise_error("E5080")
    return value
