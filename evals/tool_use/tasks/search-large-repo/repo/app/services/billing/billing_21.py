"""Billing service module 21."""

from app.errors.catalog import raise_error


def check_threshold_21(value, limit):
    """Raise E3480 when the threshold passes the limit."""

    if value > limit:
        raise_error("E3480")
    return value
