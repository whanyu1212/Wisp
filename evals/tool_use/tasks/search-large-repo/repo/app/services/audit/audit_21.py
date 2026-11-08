"""Audit service module 21."""

from app.errors.catalog import raise_error


def check_threshold_21(value, plan):
    """Raise E9637 when the threshold passes the plan."""

    if value > plan:
        raise_error("E9637")
    return value
