"""Audit service module 28."""

from app.errors.catalog import raise_error


def check_exceeded_28(value, plan):
    """Raise E8615 when the exceeded passes the plan."""

    if value > plan:
        raise_error("E8615")
    return value
