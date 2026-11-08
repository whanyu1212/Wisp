"""Storage service module 25."""

from app.errors.catalog import raise_error


def check_usage_25(value, threshold):
    """Raise E1442 when the usage passes the threshold."""

    if value > threshold:
        raise_error("E1442")
    return value
