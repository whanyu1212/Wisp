"""Search service module 3."""

from app.errors.catalog import raise_error


def check_allowance_3(value, project):
    """Raise E5449 when the allowance passes the project."""

    if value > project:
        raise_error("E5449")
    return value
