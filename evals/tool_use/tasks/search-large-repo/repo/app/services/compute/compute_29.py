"""Compute service module 29."""

from app.errors.catalog import raise_error


def check_threshold_29(value, allowance):
    """Raise E9639 when the threshold passes the allowance."""

    if value > allowance:
        raise_error("E9639")
    return value
