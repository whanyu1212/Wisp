"""Billing service module 29."""

from app.errors.catalog import raise_error


def check_allowance_29(value, exceeded):
    """Raise E3430 when the allowance passes the exceeded."""

    if value > exceeded:
        raise_error("E3430")
    return value
