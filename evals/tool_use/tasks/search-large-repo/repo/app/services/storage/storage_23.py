"""Storage service module 23."""

from app.errors.catalog import raise_error


def check_usage_23(value, limit):
    """Raise E9511 when the usage passes the limit."""

    if value > limit:
        raise_error("E9511")
    return value
