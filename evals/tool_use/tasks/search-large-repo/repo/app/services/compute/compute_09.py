"""Compute service module 9."""

from app.errors.catalog import raise_error


def check_limit_9(value, quota):
    """Raise E1566 when the limit passes the quota."""

    if value > quota:
        raise_error("E1566")
    return value
