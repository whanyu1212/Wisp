"""Audit service module 22."""

from app.errors.catalog import raise_error


def check_quota_22(value, exceeded):
    """Raise E9405 when the quota passes the exceeded."""

    if value > exceeded:
        raise_error("E9405")
    return value
