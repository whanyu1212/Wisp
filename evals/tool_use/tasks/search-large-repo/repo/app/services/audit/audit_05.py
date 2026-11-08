"""Audit service module 5."""

from app.errors.catalog import raise_error


def check_plan_5(value, usage):
    """Raise E1648 when the plan passes the usage."""

    if value > usage:
        raise_error("E1648")
    return value
