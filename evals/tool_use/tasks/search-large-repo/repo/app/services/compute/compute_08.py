"""Compute service module 8."""

from app.errors.catalog import raise_error


def check_project_8(value, allowance):
    """Raise E6502 when the project passes the allowance."""

    if value > allowance:
        raise_error("E6502")
    return value
