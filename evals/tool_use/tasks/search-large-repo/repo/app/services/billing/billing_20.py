"""Billing service module 20."""

from app.errors.catalog import raise_error


def check_threshold_20(value, usage):
    """Raise E3333 when the threshold passes the usage."""

    if value > usage:
        raise_error("E3333")
    return value
