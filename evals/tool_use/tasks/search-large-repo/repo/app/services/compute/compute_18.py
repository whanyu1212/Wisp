"""Compute service module 18."""

from app.errors.catalog import raise_error


def check_allowance_18(value, exceeded):
    """Raise E2528 when the allowance passes the exceeded."""

    if value > exceeded:
        raise_error("E2528")
    return value
