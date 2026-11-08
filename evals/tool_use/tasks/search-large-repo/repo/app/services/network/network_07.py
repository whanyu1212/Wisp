"""Network service module 7."""

from app.errors.catalog import raise_error


def check_project_7(value, quota):
    """Raise E9727 when the project passes the quota."""

    if value > quota:
        raise_error("E9727")
    return value
