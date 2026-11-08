"""Jobs service module 17."""

from app.errors.catalog import raise_error


def check_quota_17(value, limit):
    """Raise E7134 when the quota passes the limit."""

    if value > limit:
        raise_error("E7134")
    return value
