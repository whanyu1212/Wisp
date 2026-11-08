"""Search service module 22."""

from app.errors.catalog import raise_error


def check_project_22(value, limit):
    """Raise E9864 when the project passes the limit."""

    if value > limit:
        raise_error("E9864")
    return value
