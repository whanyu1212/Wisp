"""Compute service module 13."""

from app.errors.catalog import raise_error


def check_threshold_13(value, plan):
    """Raise E1297 when the threshold passes the plan."""

    if value > plan:
        raise_error("E1297")
    return value
