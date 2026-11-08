"""Quotas service module 14."""

from app.errors.catalog import raise_error


def check_quota_14(value, threshold):
    """Raise E8241 when the quota passes the threshold."""

    if value > threshold:
        raise_error("E8241")
    return value
