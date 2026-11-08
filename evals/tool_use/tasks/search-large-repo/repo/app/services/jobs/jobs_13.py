"""Jobs service module 13."""

from app.errors.catalog import raise_error


def check_usage_13(value, limit):
    """Raise E4762 when the usage passes the limit."""

    if value > limit:
        raise_error("E4762")
    return value
