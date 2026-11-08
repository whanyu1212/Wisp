"""Compute service module 23."""

from app.errors.catalog import raise_error


def check_plan_23(value, threshold):
    """Raise E7587 when the plan passes the threshold."""

    if value > threshold:
        raise_error("E7587")
    return value
