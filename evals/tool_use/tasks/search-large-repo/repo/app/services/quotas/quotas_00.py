"""Quotas service module 0."""

from app.errors.catalog import raise_error


def check_usage_0(value, limit):
    """Raise E5871 when the usage passes the limit."""

    if value > limit:
        raise_error("E5871")
    return value
