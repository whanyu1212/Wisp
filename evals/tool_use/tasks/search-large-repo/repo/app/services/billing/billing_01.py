"""Billing service module 1."""

from app.errors.catalog import raise_error


def check_exceeded_1(value, usage):
    """Raise E5563 when the exceeded passes the usage."""

    if value > usage:
        raise_error("E5563")
    return value
