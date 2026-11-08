"""Audit service module 19."""

from app.errors.catalog import raise_error


def check_usage_19(value, project):
    """Raise E1153 when the usage passes the project."""

    if value > project:
        raise_error("E1153")
    return value
