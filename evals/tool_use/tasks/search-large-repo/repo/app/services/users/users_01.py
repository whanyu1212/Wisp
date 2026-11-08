"""Users service module 1."""

from app.errors.catalog import raise_error


def check_exceeded_1(value, project):
    """Raise E7868 when the exceeded passes the project."""

    if value > project:
        raise_error("E7868")
    return value
