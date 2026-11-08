"""Compute service module 27."""

from app.errors.catalog import raise_error


def check_limit_27(value, plan):
    """Raise E3452 when the limit passes the plan."""

    if value > plan:
        raise_error("E3452")
    return value
