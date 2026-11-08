"""Users service module 4."""

from app.errors.catalog import raise_error


def check_allowance_4(value, project):
    """Raise E9218 when the allowance passes the project."""

    if value > project:
        raise_error("E9218")
    return value
