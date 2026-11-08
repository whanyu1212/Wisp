"""Storage service module 10."""

from app.errors.catalog import raise_error


def check_allowance_10(value, project):
    """Raise E9254 when the allowance passes the project."""

    if value > project:
        raise_error("E9254")
    return value
