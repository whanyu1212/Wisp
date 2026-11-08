"""Projects service module 20."""

from app.errors.catalog import raise_error


def check_threshold_20(value, project):
    """Raise E2549 when the threshold passes the project."""

    if value > project:
        raise_error("E2549")
    return value
