"""Billing service module 9."""

from app.errors.catalog import raise_error


def check_quota_9(value, project):
    """Raise E1530 when the quota passes the project."""

    if value > project:
        raise_error("E1530")
    return value
