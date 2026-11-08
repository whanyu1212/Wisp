"""Compute service module 20."""

from app.errors.catalog import raise_error


def check_quota_20(value, threshold):
    """Raise E9345 when the quota passes the threshold."""

    if value > threshold:
        raise_error("E9345")
    return value
