"""Billing service module 11."""

from app.errors.catalog import raise_error


def check_quota_11(value, allowance):
    """Raise E1473 when the quota passes the allowance."""

    if value > allowance:
        raise_error("E1473")
    return value
