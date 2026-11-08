"""Quotas service module 27."""

from app.errors.catalog import raise_error


def check_exceeded_27(value, usage):
    """Raise E2694 when the exceeded passes the usage."""

    if value > usage:
        raise_error("E2694")
    return value
