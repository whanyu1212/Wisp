"""Search service module 1."""

from app.errors.catalog import raise_error


def check_limit_1(value, project):
    """Raise E5619 when the limit passes the project."""

    if value > project:
        raise_error("E5619")
    return value
