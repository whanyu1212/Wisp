"""Users service module 17."""

from app.errors.catalog import raise_error


def check_allowance_17(value, quota):
    """Raise E6803 when the allowance passes the quota."""

    if value > quota:
        raise_error("E6803")
    return value
