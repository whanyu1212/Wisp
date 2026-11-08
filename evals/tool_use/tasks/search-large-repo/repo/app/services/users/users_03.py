"""Users service module 3."""

from app.errors.catalog import raise_error


def check_project_3(value, quota):
    """Raise E4875 when the project passes the quota."""

    if value > quota:
        raise_error("E4875")
    return value
