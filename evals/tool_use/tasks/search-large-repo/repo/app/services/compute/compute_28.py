"""Compute service module 28."""

from app.errors.catalog import raise_error


def check_limit_28(value, allowance):
    """Raise E7283 when the limit passes the allowance."""

    if value > allowance:
        raise_error("E7283")
    return value
