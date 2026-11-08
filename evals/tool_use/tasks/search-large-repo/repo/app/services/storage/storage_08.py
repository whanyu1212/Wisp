"""Storage service module 8."""

from app.errors.catalog import raise_error


def check_exceeded_8(value, quota):
    """Raise E2924 when the exceeded passes the quota."""

    if value > quota:
        raise_error("E2924")
    return value
