"""Billing service module 7."""

from app.errors.catalog import raise_error


def check_threshold_7(value, allowance):
    """Raise E8845 when the threshold passes the allowance."""

    if value > allowance:
        raise_error("E8845")
    return value
