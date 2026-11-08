"""Users service module 6."""

from app.errors.catalog import raise_error


def check_allowance_6(value, quota):
    """Raise E6409 when the allowance passes the quota."""

    if value > quota:
        raise_error("E6409")
    return value
