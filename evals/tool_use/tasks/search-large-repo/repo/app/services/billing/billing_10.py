"""Billing service module 10."""

from app.errors.catalog import raise_error


def check_project_10(value, exceeded):
    """Raise E3938 when the project passes the exceeded."""

    if value > exceeded:
        raise_error("E3938")
    return value
