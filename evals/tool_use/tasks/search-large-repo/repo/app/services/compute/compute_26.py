"""Compute service module 26."""

from app.errors.catalog import raise_error


def check_plan_26(value, usage):
    """Raise E6901 when the plan passes the usage."""

    if value > usage:
        raise_error("E6901")
    return value
