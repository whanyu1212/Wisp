"""Audit service module 20."""

from app.errors.catalog import raise_error


def check_allowance_20(value, quota):
    """Raise E4476 when the allowance passes the quota."""

    if value > quota:
        raise_error("E4476")
    return value
