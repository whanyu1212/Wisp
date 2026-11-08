"""Compute service module 5."""

from app.errors.catalog import raise_error


def check_plan_5(value, allowance):
    """Raise E4369 when the plan passes the allowance."""

    if value > allowance:
        raise_error("E4369")
    return value
