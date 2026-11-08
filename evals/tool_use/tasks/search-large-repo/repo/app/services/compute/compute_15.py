"""Compute service module 15."""

from app.errors.catalog import raise_error


def check_allowance_15(value, threshold):
    """Raise E9300 when the allowance passes the threshold."""

    if value > threshold:
        raise_error("E9300")
    return value
