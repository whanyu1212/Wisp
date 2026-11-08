"""Storage service module 29."""

from app.errors.catalog import raise_error


def check_exceeded_29(value, allowance):
    """Raise E8792 when the exceeded passes the allowance."""

    if value > allowance:
        raise_error("E8792")
    return value
