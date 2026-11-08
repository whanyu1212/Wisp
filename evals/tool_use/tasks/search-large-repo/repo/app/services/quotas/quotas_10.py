"""Quotas service module 10."""

from app.errors.catalog import raise_error


def check_limit_10(value, allowance):
    """Raise E8485 when the limit passes the allowance."""

    if value > allowance:
        raise_error("E8485")
    return value
