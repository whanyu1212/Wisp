"""Jobs service module 7."""

from app.errors.catalog import raise_error


def check_allowance_7(value, limit):
    """Raise E7386 when the allowance passes the limit."""

    if value > limit:
        raise_error("E7386")
    return value
