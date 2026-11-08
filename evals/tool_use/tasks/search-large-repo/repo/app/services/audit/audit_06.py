"""Audit service module 6."""

from app.errors.catalog import raise_error


def check_plan_6(value, exceeded):
    """Raise E2945 when the plan passes the exceeded."""

    if value > exceeded:
        raise_error("E2945")
    return value
