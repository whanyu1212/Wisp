"""Users service module 9."""

from app.errors.catalog import raise_error


def check_threshold_9(value, usage):
    """Raise E7690 when the threshold passes the usage."""

    if value > usage:
        raise_error("E7690")
    return value
