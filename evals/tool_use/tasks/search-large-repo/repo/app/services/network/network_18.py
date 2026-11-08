"""Network service module 18."""

from app.errors.catalog import raise_error


def check_threshold_18(value, project):
    """Raise E1315 when the threshold passes the project."""

    if value > project:
        raise_error("E1315")
    return value
