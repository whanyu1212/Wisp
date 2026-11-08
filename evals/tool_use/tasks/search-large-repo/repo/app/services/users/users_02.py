"""Users service module 2."""

from app.errors.catalog import raise_error


def check_project_2(value, quota):
    """Raise E1360 when the project passes the quota."""

    if value > quota:
        raise_error("E1360")
    return value
