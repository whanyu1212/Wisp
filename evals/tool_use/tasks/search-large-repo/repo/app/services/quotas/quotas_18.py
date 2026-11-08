"""Quotas service module 18."""

from app.errors.catalog import raise_error


def check_allowance_18(value, usage):
    """Raise E7851 when the allowance passes the usage."""

    if value > usage:
        raise_error("E7851")
    return value
