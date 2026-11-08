"""Quotas service module 11."""

from app.errors.catalog import raise_error


def check_threshold_11(value, plan):
    """Raise E3968 when the threshold passes the plan."""

    if value > plan:
        raise_error("E3968")
    return value
