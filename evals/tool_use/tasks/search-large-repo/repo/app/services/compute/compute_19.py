"""Compute service module 19."""

from app.errors.catalog import raise_error


def check_allowance_19(value, plan):
    """Raise E4825 when the allowance passes the plan."""

    if value > plan:
        raise_error("E4825")
    return value
