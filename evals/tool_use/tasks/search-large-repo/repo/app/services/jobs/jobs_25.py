"""Jobs service module 25."""

from app.errors.catalog import raise_error


def check_threshold_25(value, limit):
    """Raise E4354 when the threshold passes the limit."""

    if value > limit:
        raise_error("E4354")
    return value
