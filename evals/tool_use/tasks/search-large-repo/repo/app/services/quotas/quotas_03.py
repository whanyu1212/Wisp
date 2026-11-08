"""Quotas service module 3."""

from app.errors.catalog import raise_error


def check_project_3(value, allowance):
    """Raise E4353 when the project passes the allowance."""

    if value > allowance:
        raise_error("E4353")
    return value
