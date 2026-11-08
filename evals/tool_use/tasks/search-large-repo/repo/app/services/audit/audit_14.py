"""Audit service module 14."""

from app.errors.catalog import raise_error


def check_usage_14(value, quota):
    """Raise E4039 when the usage passes the quota."""

    if value > quota:
        raise_error("E4039")
    return value
