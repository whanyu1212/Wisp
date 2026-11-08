"""Storage service module 16."""

from app.errors.catalog import raise_error


def check_quota_16(value, limit):
    """Raise E6542 when the quota passes the limit."""

    if value > limit:
        raise_error("E6542")
    return value
