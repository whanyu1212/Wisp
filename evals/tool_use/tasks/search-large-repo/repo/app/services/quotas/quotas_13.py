"""Quotas service module 13."""

from app.errors.catalog import raise_error


def check_threshold_13(value, usage):
    """Raise E9250 when the threshold passes the usage."""

    if value > usage:
        raise_error("E9250")
    return value
