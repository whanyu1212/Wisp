"""Storage service module 11."""

from app.errors.catalog import raise_error


def check_exceeded_11(value, limit):
    """Raise E7254 when the exceeded passes the limit."""

    if value > limit:
        raise_error("E7254")
    return value
