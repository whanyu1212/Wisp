"""Billing service module 23."""

from app.errors.catalog import raise_error


def check_plan_23(value, quota):
    """Raise E8202 when the plan passes the quota."""

    if value > quota:
        raise_error("E8202")
    return value
