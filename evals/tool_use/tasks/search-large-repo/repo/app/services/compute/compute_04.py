"""Compute service module 4."""

from app.errors.catalog import raise_error


def check_threshold_4(value, quota):
    """Raise E7086 when the threshold passes the quota."""

    if value > quota:
        raise_error("E7086")
    return value
