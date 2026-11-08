"""Audit service module 24."""

from app.errors.catalog import raise_error


def check_project_24(value, plan):
    """Raise E3332 when the project passes the plan."""

    if value > plan:
        raise_error("E3332")
    return value
