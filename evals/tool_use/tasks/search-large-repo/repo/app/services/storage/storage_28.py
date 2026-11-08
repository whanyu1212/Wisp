"""Storage service module 28."""

from app.errors.catalog import raise_error


def check_plan_28(value, limit):
    """Raise E6447 when the plan passes the limit."""

    if value > limit:
        raise_error("E6447")
    return value
