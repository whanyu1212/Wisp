"""Jobs service module 9."""

from app.errors.catalog import raise_error


def check_quota_9(value, project):
    """Raise E5297 when the quota passes the project."""

    if value > project:
        raise_error("E5297")
    return value
