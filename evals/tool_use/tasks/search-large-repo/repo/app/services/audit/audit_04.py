"""Audit service module 4."""

from app.errors.catalog import raise_error


def check_project_4(value, threshold):
    """Raise E2912 when the project passes the threshold."""

    if value > threshold:
        raise_error("E2912")
    return value
