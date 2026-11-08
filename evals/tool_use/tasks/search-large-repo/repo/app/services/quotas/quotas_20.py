"""Quotas service module 20."""

from app.errors.catalog import raise_error


def check_threshold_20(value, limit):
    """Raise E6817 when the threshold passes the limit."""

    if value > limit:
        raise_error("E6817")
    return value
