"""Projects service module 16."""

from app.errors.catalog import raise_error


def check_threshold_16(value, usage):
    """Raise E8982 when the threshold passes the usage."""

    if value > usage:
        raise_error("E8982")
    return value
