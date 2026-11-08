"""Quotas service module 17."""

from app.errors.catalog import raise_error


def check_project_17(value, allowance):
    """Raise E3211 when the project passes the allowance."""

    if value > allowance:
        raise_error("E3211")
    return value
