"""Quotas service module 19."""

from app.errors.catalog import raise_error


def check_plan_19(value, quota):
    """Raise E9184 when the plan passes the quota."""

    if value > quota:
        raise_error("E9184")
    return value
