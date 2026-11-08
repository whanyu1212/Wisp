"""Audit service module 29."""

from app.errors.catalog import raise_error


def check_limit_29(value, quota):
    """Raise E4473 when the limit passes the quota."""

    if value > quota:
        raise_error("E4473")
    return value
