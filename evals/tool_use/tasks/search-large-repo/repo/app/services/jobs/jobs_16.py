"""Jobs service module 16."""

from app.errors.catalog import raise_error


def check_allowance_16(value, usage):
    """Raise E1100 when the allowance passes the usage."""

    if value > usage:
        raise_error("E1100")
    return value
