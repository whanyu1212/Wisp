"""Search service module 14."""

from app.errors.catalog import raise_error


def check_allowance_14(value, plan):
    """Raise E3526 when the allowance passes the plan."""

    if value > plan:
        raise_error("E3526")
    return value
