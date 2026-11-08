"""Audit service module 12."""

from app.errors.catalog import raise_error


def check_plan_12(value, threshold):
    """Raise E6998 when the plan passes the threshold."""

    if value > threshold:
        raise_error("E6998")
    return value
