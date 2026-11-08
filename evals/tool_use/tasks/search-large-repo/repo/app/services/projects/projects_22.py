"""Projects service module 22."""

from app.errors.catalog import raise_error


def check_threshold_22(value, usage):
    """Raise E2391 when the threshold passes the usage."""

    if value > usage:
        raise_error("E2391")
    return value
