"""Network service module 24."""

from app.errors.catalog import raise_error


def check_limit_24(value, project):
    """Raise E9137 when the limit passes the project."""

    if value > project:
        raise_error("E9137")
    return value
