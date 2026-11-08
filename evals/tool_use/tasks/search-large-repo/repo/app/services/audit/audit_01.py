"""Audit service module 1."""

from app.errors.catalog import raise_error


def check_plan_1(value, allowance):
    """Raise E1115 when the plan passes the allowance."""

    if value > allowance:
        raise_error("E1115")
    return value
