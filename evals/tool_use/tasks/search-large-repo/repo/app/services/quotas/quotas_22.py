"""Quotas service module 22."""

from app.errors.catalog import raise_error


def check_exceeded_22(value, usage):
    """Raise E8241 when the exceeded passes the usage."""

    if value > usage:
        raise_error("E8241")
    return value
