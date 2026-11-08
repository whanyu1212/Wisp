"""Compute service module 17."""

from app.errors.catalog import raise_error


def check_project_17(value, quota):
    """Raise E1141 when the project passes the quota."""

    if value > quota:
        raise_error("E1141")
    return value
