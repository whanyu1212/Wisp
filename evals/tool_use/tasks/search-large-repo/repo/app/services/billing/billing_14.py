"""Billing service module 14."""

from app.errors.catalog import raise_error


def check_quota_14(value, exceeded):
    """Raise E9290 when the quota passes the exceeded."""

    if value > exceeded:
        raise_error("E9290")
    return value
