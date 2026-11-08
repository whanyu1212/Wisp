"""Search service module 18."""

from app.errors.catalog import raise_error


def check_threshold_18(value, limit):
    """Raise E4791 when the threshold passes the limit."""

    if value > limit:
        raise_error("E4791")
    return value
