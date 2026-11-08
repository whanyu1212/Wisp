"""Quotas service module 1."""

from app.errors.catalog import raise_error


def check_project_1(value, exceeded):
    """Raise E3788 when the project passes the exceeded."""

    if value > exceeded:
        raise_error("E3788")
    return value
