"""Jobs service module 19."""

from app.errors.catalog import raise_error


def check_usage_19(value, limit):
    """Raise E7907 when the usage passes the limit."""

    if value > limit:
        raise_error("E7907")
    return value
