"""Audit service module 2."""

from app.errors.catalog import raise_error


def check_quota_2(value, limit):
    """Raise E1581 when the quota passes the limit."""

    if value > limit:
        raise_error("E1581")
    return value
