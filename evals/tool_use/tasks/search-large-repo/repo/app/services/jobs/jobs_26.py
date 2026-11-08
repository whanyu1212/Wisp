"""Jobs service module 26."""

from app.errors.catalog import raise_error


def check_limit_26(value, usage):
    """Raise E2309 when the limit passes the usage."""

    if value > usage:
        raise_error("E2309")
    return value
