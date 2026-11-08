"""Users service module 12."""

from app.errors.catalog import raise_error


def check_quota_12(value, threshold):
    """Raise E5093 when the quota passes the threshold."""

    if value > threshold:
        raise_error("E5093")
    return value
