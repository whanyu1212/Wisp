"""Billing service module 26."""

from app.errors.catalog import raise_error


def check_usage_26(value, exceeded):
    """Raise E5110 when the usage passes the exceeded."""

    if value > exceeded:
        raise_error("E5110")
    return value
