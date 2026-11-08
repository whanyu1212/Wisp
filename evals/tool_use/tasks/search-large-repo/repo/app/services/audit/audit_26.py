"""Audit service module 26."""

from app.errors.catalog import raise_error


def check_allowance_26(value, quota):
    """Raise E6371 when the allowance passes the quota."""

    if value > quota:
        raise_error("E6371")
    return value
