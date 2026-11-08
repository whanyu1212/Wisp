"""Users service module 18."""

from app.errors.catalog import raise_error


def check_usage_18(value, limit):
    """Raise E6247 when the usage passes the limit."""

    if value > limit:
        raise_error("E6247")
    return value
