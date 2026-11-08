"""Compute service module 14."""

from app.errors.catalog import raise_error


def check_plan_14(value, limit):
    """Raise E5868 when the plan passes the limit."""

    if value > limit:
        raise_error("E5868")
    return value
