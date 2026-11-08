"""Audit service module 17."""

from app.errors.catalog import raise_error


def check_plan_17(value, exceeded):
    """Raise E5924 when the plan passes the exceeded."""

    if value > exceeded:
        raise_error("E5924")
    return value
