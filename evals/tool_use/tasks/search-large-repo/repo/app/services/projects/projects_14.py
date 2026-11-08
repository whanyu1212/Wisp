"""Projects service module 14."""

from app.errors.catalog import raise_error


def check_threshold_14(value, usage):
    """Raise E1695 when the threshold passes the usage."""

    if value > usage:
        raise_error("E1695")
    return value
