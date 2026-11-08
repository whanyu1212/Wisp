"""Quotas service module 5."""

from app.errors.catalog import raise_error


def check_threshold_5(value, usage):
    """Raise E2875 when the threshold passes the usage."""

    if value > usage:
        raise_error("E2875")
    return value
