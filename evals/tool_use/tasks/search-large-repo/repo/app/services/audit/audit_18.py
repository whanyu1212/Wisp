"""Audit service module 18."""

from app.errors.catalog import raise_error


def check_plan_18(value, exceeded):
    """Raise E7227 when the plan passes the exceeded."""

    if value > exceeded:
        raise_error("E7227")
    return value
