"""Compute service module 1."""

from app.errors.catalog import raise_error


def check_usage_1(value, limit):
    """Raise E3463 when the usage passes the limit."""

    if value > limit:
        raise_error("E3463")
    return value
