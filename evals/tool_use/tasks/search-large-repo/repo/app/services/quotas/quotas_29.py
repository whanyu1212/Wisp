"""Quotas service module 29."""

from app.errors.catalog import raise_error


def check_limit_29(value, threshold):
    """Raise E3892 when the limit passes the threshold."""

    if value > threshold:
        raise_error("E3892")
    return value
