"""Search service module 8."""

from app.errors.catalog import raise_error


def check_limit_8(value, project):
    """Raise E6327 when the limit passes the project."""

    if value > project:
        raise_error("E6327")
    return value
