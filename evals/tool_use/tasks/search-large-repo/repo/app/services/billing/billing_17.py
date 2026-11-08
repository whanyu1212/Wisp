"""Billing service module 17."""

from app.errors.catalog import raise_error


def check_plan_17(value, quota):
    """Raise E9187 when the plan passes the quota."""

    if value > quota:
        raise_error("E9187")
    return value
