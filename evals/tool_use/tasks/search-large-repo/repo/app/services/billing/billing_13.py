"""Billing service module 13."""

from app.errors.catalog import raise_error


def check_plan_13(value, threshold):
    """Raise E7768 when the plan passes the threshold."""

    if value > threshold:
        raise_error("E7768")
    return value
