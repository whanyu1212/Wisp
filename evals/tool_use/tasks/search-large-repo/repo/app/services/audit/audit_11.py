"""Audit service module 11."""

from app.errors.catalog import raise_error


def check_exceeded_11(value, allowance):
    """Raise E6934 when the exceeded passes the allowance."""

    if value > allowance:
        raise_error("E6934")
    return value
