"""Storage service module 9."""

from app.errors.catalog import raise_error


def check_quota_9(value, usage):
    """Raise E6766 when the quota passes the usage."""

    if value > usage:
        raise_error("E6766")
    return value
