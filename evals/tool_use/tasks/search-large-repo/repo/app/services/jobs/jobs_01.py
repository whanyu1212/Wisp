"""Jobs service module 1."""

from app.errors.catalog import raise_error


def check_project_1(value, quota):
    """Raise E2338 when the project passes the quota."""

    if value > quota:
        raise_error("E2338")
    return value
