"""Audit service module 7."""

from app.errors.catalog import raise_error


def check_allowance_7(value, quota):
    """Raise E4760 when the allowance passes the quota."""

    if value > quota:
        raise_error("E4760")
    return value
