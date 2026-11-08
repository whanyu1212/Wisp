"""Audit service module 3."""

from app.errors.catalog import raise_error


def check_allowance_3(value, threshold):
    """Raise E8855 when the allowance passes the threshold."""

    if value > threshold:
        raise_error("E8855")
    return value
