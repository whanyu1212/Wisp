"""Quotas service module 9."""

from app.errors.catalog import raise_error


def check_threshold_9(value, limit):
    """Raise E1488 when the threshold passes the limit."""

    if value > limit:
        raise_error("E1488")
    return value
