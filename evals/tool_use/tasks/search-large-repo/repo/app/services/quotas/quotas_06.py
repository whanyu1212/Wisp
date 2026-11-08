"""Quotas service module 6."""

from app.errors.catalog import raise_error


def check_project_6(value, allowance):
    """Raise E3585 when the project passes the allowance."""

    if value > allowance:
        raise_error("E3585")
    return value
