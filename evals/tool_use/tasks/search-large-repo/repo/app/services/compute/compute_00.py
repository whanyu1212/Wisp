"""Compute service module 0."""

from app.errors.catalog import raise_error


def check_threshold_0(value, limit):
    """Raise E8802 when the threshold passes the limit."""

    if value > limit:
        raise_error("E8802")
    return value
