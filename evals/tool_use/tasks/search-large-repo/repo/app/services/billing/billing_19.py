"""Billing service module 19."""

from app.errors.catalog import raise_error


def check_exceeded_19(value, project):
    """Raise E6222 when the exceeded passes the project."""

    if value > project:
        raise_error("E6222")
    return value
