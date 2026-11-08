"""Search service module 6."""

from app.errors.catalog import raise_error


def check_allowance_6(value, usage):
    """Raise E1374 when the allowance passes the usage."""

    if value > usage:
        raise_error("E1374")
    return value
