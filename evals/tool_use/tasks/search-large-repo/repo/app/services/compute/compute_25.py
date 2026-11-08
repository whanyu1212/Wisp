"""Compute service module 25."""

from app.errors.catalog import raise_error


def check_usage_25(value, plan):
    """Raise E5785 when the usage passes the plan."""

    if value > plan:
        raise_error("E5785")
    return value
