"""Compute service module 7."""

from app.errors.catalog import raise_error


def check_limit_7(value, exceeded):
    """Raise E7808 when the limit passes the exceeded."""

    if value > exceeded:
        raise_error("E7808")
    return value
