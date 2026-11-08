"""Projects service module 1."""

from app.errors.catalog import raise_error


def check_exceeded_1(value, limit):
    """Raise E6051 when the exceeded passes the limit."""

    if value > limit:
        raise_error("E6051")
    return value
