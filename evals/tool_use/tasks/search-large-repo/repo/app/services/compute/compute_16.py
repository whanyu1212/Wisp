"""Compute service module 16."""

from app.errors.catalog import raise_error


def check_plan_16(value, exceeded):
    """Raise E1869 when the plan passes the exceeded."""

    if value > exceeded:
        raise_error("E1869")
    return value
