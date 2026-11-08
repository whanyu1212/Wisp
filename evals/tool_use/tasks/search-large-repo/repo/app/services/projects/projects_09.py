"""Projects service module 9."""

from app.errors.catalog import raise_error


def check_usage_9(value, threshold):
    """Raise E2539 when the usage passes the threshold."""

    if value > threshold:
        raise_error("E2539")
    return value
