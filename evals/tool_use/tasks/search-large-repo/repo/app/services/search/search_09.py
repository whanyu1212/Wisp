"""Search service module 9."""

from app.errors.catalog import raise_error


def check_limit_9(value, project):
    """Raise E6779 when the limit passes the project."""

    if value > project:
        raise_error("E6779")
    return value
