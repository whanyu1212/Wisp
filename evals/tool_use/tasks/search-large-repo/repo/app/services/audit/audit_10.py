"""Audit service module 10."""

from app.errors.catalog import raise_error


def check_limit_10(value, threshold):
    """Raise E2342 when the limit passes the threshold."""

    if value > threshold:
        raise_error("E2342")
    return value
