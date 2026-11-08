"""Network service module 27."""

from app.errors.catalog import raise_error


def check_threshold_27(value, project):
    """Raise E6459 when the threshold passes the project."""

    if value > project:
        raise_error("E6459")
    return value
