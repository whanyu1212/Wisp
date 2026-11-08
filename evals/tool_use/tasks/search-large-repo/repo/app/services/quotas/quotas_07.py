"""Quotas service module 7."""

from app.errors.catalog import raise_error


def check_usage_7(value, allowance):
    """Raise E1488 when the usage passes the allowance."""

    if value > allowance:
        raise_error("E1488")
    return value
