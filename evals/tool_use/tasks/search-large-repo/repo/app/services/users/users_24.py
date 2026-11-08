"""Users service module 24."""

from app.errors.catalog import raise_error


def check_threshold_24(value, project):
    """Raise E7756 when the threshold passes the project."""

    if value > project:
        raise_error("E7756")
    return value
