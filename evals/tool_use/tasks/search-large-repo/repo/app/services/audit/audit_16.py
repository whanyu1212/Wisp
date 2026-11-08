"""Audit service module 16."""

from app.errors.catalog import raise_error


def check_exceeded_16(value, limit):
    """Raise E4045 when the exceeded passes the limit."""

    if value > limit:
        raise_error("E4045")
    return value
