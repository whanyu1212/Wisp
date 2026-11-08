"""Storage service module 19."""

from app.errors.catalog import raise_error


def check_quota_19(value, project):
    """Raise E5887 when the quota passes the project."""

    if value > project:
        raise_error("E5887")
    return value
