"""Quotas service module 15."""

from app.errors.catalog import raise_error


def check_plan_15(value, allowance):
    """Raise E4193 when the plan passes the allowance."""

    if value > allowance:
        raise_error("E4193")
    return value
