"""Storage service module 5."""

from app.errors.catalog import raise_error


def check_allowance_5(value, threshold):
    """Raise E4780 when the allowance passes the threshold."""

    if value > threshold:
        raise_error("E4780")
    return value
