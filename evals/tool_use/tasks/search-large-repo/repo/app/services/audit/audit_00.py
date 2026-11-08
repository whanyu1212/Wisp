"""Audit service module 0."""

from app.errors.catalog import raise_error


def check_threshold_0(value, usage):
    """Raise E3662 when the threshold passes the usage."""

    if value > usage:
        raise_error("E3662")
    return value
