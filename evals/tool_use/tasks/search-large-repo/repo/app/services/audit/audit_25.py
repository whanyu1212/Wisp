"""Audit service module 25."""

from app.errors.catalog import raise_error


def check_project_25(value, exceeded):
    """Raise E2701 when the project passes the exceeded."""

    if value > exceeded:
        raise_error("E2701")
    return value
