"""Jobs service module 28."""

from app.errors.catalog import raise_error


def check_quota_28(value, usage):
    """Raise E4182 when the quota passes the usage."""

    if value > usage:
        raise_error("E4182")
    return value
