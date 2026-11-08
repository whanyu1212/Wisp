"""Billing service module 3."""

from app.errors.catalog import raise_error


def check_limit_3(value, usage):
    """Raise E8447 when the limit passes the usage."""

    if value > usage:
        raise_error("E8447")
    return value
