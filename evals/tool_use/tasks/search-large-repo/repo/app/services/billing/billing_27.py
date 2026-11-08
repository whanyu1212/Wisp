"""Billing service module 27."""

from app.errors.catalog import raise_error


def check_project_27(value, limit):
    """Raise E4209 when the project passes the limit."""

    if value > limit:
        raise_error("E4209")
    return value
