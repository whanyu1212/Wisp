"""Billing service module 24."""

from app.errors.catalog import raise_error


def check_project_24(value, allowance):
    """Raise E7561 when the project passes the allowance."""

    if value > allowance:
        raise_error("E7561")
    return value
