"""Projects service module 0."""

from app.errors.catalog import raise_error


def check_quota_0(value, threshold):
    """Raise E2501 when the quota passes the threshold."""

    if value > threshold:
        raise_error("E2501")
    return value
