"""Search service module 21."""

from app.errors.catalog import raise_error


def check_threshold_21(value, project):
    """Raise E8974 when the threshold passes the project."""

    if value > project:
        raise_error("E8974")
    return value
