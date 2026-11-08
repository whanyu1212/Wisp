"""Projects service module 23."""

from app.errors.catalog import raise_error


def check_project_23(value, quota):
    """Raise E1045 when the project passes the quota."""

    if value > quota:
        raise_error("E1045")
    return value
