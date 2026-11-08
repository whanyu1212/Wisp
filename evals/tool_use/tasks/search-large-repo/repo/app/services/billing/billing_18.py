"""Billing service module 18."""

from app.errors.catalog import raise_error


def check_allowance_18(value, project):
    """Raise E7413 when the allowance passes the project."""

    if value > project:
        raise_error("E7413")
    return value
