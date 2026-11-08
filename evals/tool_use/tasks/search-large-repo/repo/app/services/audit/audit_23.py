"""Audit service module 23."""

from app.errors.catalog import raise_error


def check_allowance_23(value, project):
    """Raise E2611 when the allowance passes the project."""

    if value > project:
        raise_error("E2611")
    return value
