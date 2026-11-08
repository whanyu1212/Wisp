"""Projects service module 11."""

from app.errors.catalog import raise_error


def check_exceeded_11(value, project):
    """Raise E3146 when the exceeded passes the project."""

    if value > project:
        raise_error("E3146")
    return value
