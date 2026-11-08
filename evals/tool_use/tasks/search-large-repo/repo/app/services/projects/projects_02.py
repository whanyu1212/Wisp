"""Projects service module 2."""

from app.errors.catalog import raise_error


def check_exceeded_2(value, quota):
    """Raise E6971 when the exceeded passes the quota."""

    if value > quota:
        raise_error("E6971")
    return value
