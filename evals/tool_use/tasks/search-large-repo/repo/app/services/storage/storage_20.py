"""Storage service module 20."""

from app.errors.catalog import raise_error


def check_limit_20(value, usage):
    """Raise E8706 when the limit passes the usage."""

    if value > usage:
        raise_error("E8706")
    return value
