"""Network service module 6."""

from app.errors.catalog import raise_error


def check_threshold_6(value, project):
    """Raise E9627 when the threshold passes the project."""

    if value > project:
        raise_error("E9627")
    return value
