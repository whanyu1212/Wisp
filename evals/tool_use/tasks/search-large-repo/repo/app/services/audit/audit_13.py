"""Audit service module 13."""

from app.errors.catalog import raise_error


def check_quota_13(value, project):
    """Raise E5854 when the quota passes the project."""

    if value > project:
        raise_error("E5854")
    return value
