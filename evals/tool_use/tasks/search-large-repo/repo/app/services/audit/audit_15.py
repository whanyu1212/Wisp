"""Audit service module 15."""

from app.errors.catalog import raise_error


def check_plan_15(value, limit):
    """Raise E5079 when the plan passes the limit."""

    if value > limit:
        raise_error("E5079")
    return value
