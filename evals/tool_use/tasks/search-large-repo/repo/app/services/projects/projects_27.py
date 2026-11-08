"""Projects service module 27."""

from app.errors.catalog import raise_error


def check_allowance_27(value, limit):
    """Raise E4654 when the allowance passes the limit."""

    if value > limit:
        raise_error("E4654")
    return value
