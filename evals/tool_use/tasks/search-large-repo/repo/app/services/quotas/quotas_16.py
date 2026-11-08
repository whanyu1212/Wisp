"""Quotas service module 16."""

from app.errors.catalog import raise_error


def check_limit_16(value, exceeded):
    """Raise E1857 when the limit passes the exceeded."""

    if value > exceeded:
        raise_error("E1857")
    return value
