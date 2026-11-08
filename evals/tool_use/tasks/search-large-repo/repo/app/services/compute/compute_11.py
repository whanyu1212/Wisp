"""Compute service module 11."""

from app.errors.catalog import raise_error


def check_usage_11(value, limit):
    """Raise E7578 when the usage passes the limit."""

    if value > limit:
        raise_error("E7578")
    return value
