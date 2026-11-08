"""Jobs service module 24."""

from app.errors.catalog import raise_error


def check_quota_24(value, threshold):
    """Raise E2950 when the quota passes the threshold."""

    if value > threshold:
        raise_error("E2950")
    return value
