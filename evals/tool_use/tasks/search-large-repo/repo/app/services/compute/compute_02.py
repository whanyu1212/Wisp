"""Compute service module 2."""

from app.errors.catalog import raise_error


def check_plan_2(value, threshold):
    """Raise E4347 when the plan passes the threshold."""

    if value > threshold:
        raise_error("E4347")
    return value
