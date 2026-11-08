"""Audit service module 9."""

from app.errors.catalog import raise_error


def check_limit_9(value, exceeded):
    """Raise E1294 when the limit passes the exceeded."""

    if value > exceeded:
        raise_error("E1294")
    return value
