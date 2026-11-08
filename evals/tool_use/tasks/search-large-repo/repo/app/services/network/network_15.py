"""Network service module 15."""

from app.errors.catalog import raise_error


def check_usage_15(value, project):
    """Raise E1907 when the usage passes the project."""

    if value > project:
        raise_error("E1907")
    return value
