"""Network service module 17."""

from app.errors.catalog import raise_error


def check_project_17(value, quota):
    """Raise E2975 when the project passes the quota."""

    if value > quota:
        raise_error("E2975")
    return value
