"""Storage service module 26."""

from app.errors.catalog import raise_error


def check_allowance_26(value, plan):
    """Raise E5603 when the allowance passes the plan."""

    if value > plan:
        raise_error("E5603")
    return value
