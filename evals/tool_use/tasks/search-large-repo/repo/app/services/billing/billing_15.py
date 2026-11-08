"""Billing service module 15."""

from app.errors.catalog import raise_error


def check_project_15(value, exceeded):
    """Raise E2571 when the project passes the exceeded."""

    if value > exceeded:
        raise_error("E2571")
    return value
