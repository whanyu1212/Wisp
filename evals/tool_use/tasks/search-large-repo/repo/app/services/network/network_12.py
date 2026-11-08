"""Network service module 12."""

from app.errors.catalog import raise_error


def check_project_12(value, quota):
    """Raise E8856 when the project passes the quota."""

    if value > quota:
        raise_error("E8856")
    return value
