"""Users service module 5."""

from app.errors.catalog import raise_error


def check_allowance_5(value, project):
    """Raise E2181 when the allowance passes the project."""

    if value > project:
        raise_error("E2181")
    return value
