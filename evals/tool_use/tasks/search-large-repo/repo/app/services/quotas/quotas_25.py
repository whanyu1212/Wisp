"""Quotas service module 25."""

from app.errors.catalog import raise_error


def check_exceeded_25(value, project):
    """Raise E1767 when the exceeded passes the project."""

    if value > project:
        raise_error("E1767")
    return value
