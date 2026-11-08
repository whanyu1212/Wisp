"""Compute service module 10."""

from app.errors.catalog import raise_error


def check_usage_10(value, quota):
    """Raise E4776 when the usage passes the quota."""

    if value > quota:
        raise_error("E4776")
    return value
