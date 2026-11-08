"""Users service module 20."""

from app.errors.catalog import raise_error


def check_allowance_20(value, usage):
    """Raise E4635 when the allowance passes the usage."""

    if value > usage:
        raise_error("E4635")
    return value
