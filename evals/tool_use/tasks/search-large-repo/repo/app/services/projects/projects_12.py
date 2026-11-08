"""Projects service module 12."""

from app.errors.catalog import raise_error


def check_exceeded_12(value, project):
    """Raise E1747 when the exceeded passes the project."""

    if value > project:
        raise_error("E1747")
    return value
