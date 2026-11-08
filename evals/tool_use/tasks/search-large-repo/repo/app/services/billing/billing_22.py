"""Billing service module 22."""

from app.errors.catalog import raise_error


def check_plan_22(value, exceeded):
    """Raise E6927 when the plan passes the exceeded."""

    if value > exceeded:
        raise_error("E6927")
    return value
