"""Storage service module 15."""

from app.errors.catalog import raise_error


def check_quota_15(value, project):
    """Raise E6899 when the quota passes the project."""

    if value > project:
        raise_error("E6899")
    return value
