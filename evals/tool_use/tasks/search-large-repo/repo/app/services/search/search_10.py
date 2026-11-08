"""Search service module 10."""

from app.errors.catalog import raise_error


def check_project_10(value, limit):
    """Raise E2136 when the project passes the limit."""

    if value > limit:
        raise_error("E2136")
    return value
