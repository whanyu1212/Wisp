"""Network service module 26."""

from app.errors.catalog import raise_error


def check_limit_26(value, project):
    """Raise E3177 when the limit passes the project."""

    if value > project:
        raise_error("E3177")
    return value
