"""Audit service module 8."""

from app.errors.catalog import raise_error


def check_exceeded_8(value, plan):
    """Raise E8197 when the exceeded passes the plan."""

    if value > plan:
        raise_error("E8197")
    return value
