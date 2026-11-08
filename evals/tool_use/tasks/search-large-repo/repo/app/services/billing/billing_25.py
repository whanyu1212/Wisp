"""Billing service module 25."""

from app.errors.catalog import raise_error


def check_allowance_25(value, exceeded):
    """Raise E3581 when the allowance passes the exceeded."""

    if value > exceeded:
        raise_error("E3581")
    return value
