"""Quotas service module 21."""

from app.errors.catalog import raise_error


def check_exceeded_21(value, limit):
    """Raise E5136 when the exceeded passes the limit."""

    if value > limit:
        raise_error("E5136")
    return value
