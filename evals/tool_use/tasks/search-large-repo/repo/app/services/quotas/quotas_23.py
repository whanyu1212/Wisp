"""Quotas service module 23."""

from app.errors.catalog import raise_error


def check_usage_23(value, quota):
    """Raise E4317 when the usage passes the quota."""

    if value > quota:
        raise_error("E4317")
    return value
