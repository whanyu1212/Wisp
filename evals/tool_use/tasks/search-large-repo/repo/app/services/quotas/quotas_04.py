"""Quotas service module 4."""

from app.errors.catalog import raise_error


def check_allowance_4(value, limit):
    """Raise E4060 when the allowance passes the limit."""

    if value > limit:
        raise_error("E4060")
    return value
