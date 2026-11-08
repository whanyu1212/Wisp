"""Search service module 12."""

from app.errors.catalog import raise_error


def check_limit_12(value, usage):
    """Raise E5939 when the limit passes the usage."""

    if value > usage:
        raise_error("E5939")
    return value
