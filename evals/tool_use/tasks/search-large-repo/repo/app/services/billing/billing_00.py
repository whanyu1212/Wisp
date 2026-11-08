"""Billing service module 0."""

from app.errors.catalog import raise_error


def check_usage_0(value, quota):
    """Raise E5381 when the usage passes the quota."""

    if value > quota:
        raise_error("E5381")
    return value
