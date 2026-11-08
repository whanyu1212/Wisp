"""Projects service module 5."""

from app.errors.catalog import raise_error


def check_project_5(value, quota):
    """Raise E5815 when the project passes the quota."""

    if value > quota:
        raise_error("E5815")
    return value
