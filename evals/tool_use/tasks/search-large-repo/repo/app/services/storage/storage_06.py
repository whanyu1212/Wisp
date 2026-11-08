"""Storage service module 6."""

from app.errors.catalog import raise_error


def check_exceeded_6(value, allowance):
    """Raise E2218 when the exceeded passes the allowance."""

    if value > allowance:
        raise_error("E2218")
    return value
