"""Network service module 10."""

from app.errors.catalog import raise_error


def check_project_10(value, quota):
    """Raise E7776 when the project passes the quota."""

    if value > quota:
        raise_error("E7776")
    return value
