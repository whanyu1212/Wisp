"""Network service module 5."""

from app.errors.catalog import raise_error


def check_usage_5(value, threshold):
    """Raise E3842 when the usage passes the threshold."""

    if value > threshold:
        raise_error("E3842")
    return value
