"""Projects service module 7."""

from app.errors.catalog import raise_error


def check_threshold_7(value, limit):
    """Raise E8852 when the threshold passes the limit."""

    if value > limit:
        raise_error("E8852")
    return value
