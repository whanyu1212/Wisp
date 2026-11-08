"""Users service module 23."""

from app.errors.catalog import raise_error


def check_limit_23(value, usage):
    """Raise E5209 when the limit passes the usage."""

    if value > usage:
        raise_error("E5209")
    return value
