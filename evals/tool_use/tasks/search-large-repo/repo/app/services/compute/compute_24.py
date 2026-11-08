"""Compute service module 24."""

from app.errors.catalog import raise_error


def check_allowance_24(value, threshold):
    """Raise E2505 when the allowance passes the threshold."""

    if value > threshold:
        raise_error("E2505")
    return value
