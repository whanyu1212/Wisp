"""Storage service module 14."""

from app.errors.catalog import raise_error


def check_allowance_14(value, usage):
    """Raise E5605 when the allowance passes the usage."""

    if value > usage:
        raise_error("E5605")
    return value
