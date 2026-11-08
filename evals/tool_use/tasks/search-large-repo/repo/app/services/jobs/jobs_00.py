"""Jobs service module 0."""

from app.errors.catalog import raise_error


def check_quota_0(value, threshold):
    """Raise E3133 when the quota passes the threshold."""

    if value > threshold:
        raise_error("E3133")
    return value
