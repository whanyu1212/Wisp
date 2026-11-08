"""Network service module 19."""

from app.errors.catalog import raise_error


def check_quota_19(value, threshold):
    """Raise E8278 when the quota passes the threshold."""

    if value > threshold:
        raise_error("E8278")
    return value
