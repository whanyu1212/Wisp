"""Storage service module 4."""

from app.errors.catalog import raise_error


def check_threshold_4(value, limit):
    """Raise E3164 when the threshold passes the limit."""

    if value > limit:
        raise_error("E3164")
    return value
