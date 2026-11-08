"""Jobs service module 6."""

from app.errors.catalog import raise_error


def check_quota_6(value, threshold):
    """Raise E9781 when the quota passes the threshold."""

    if value > threshold:
        raise_error("E9781")
    return value
