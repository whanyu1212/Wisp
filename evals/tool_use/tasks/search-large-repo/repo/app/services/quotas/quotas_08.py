"""Quotas service module 8."""

from app.errors.catalog import raise_error


def check_limit_8(value, usage):
    """Raise E1689 when the limit passes the usage."""

    if value > usage:
        raise_error("E1689")
    return value
