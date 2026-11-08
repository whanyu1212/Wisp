"""Search service module 7."""

from app.errors.catalog import raise_error


def check_exceeded_7(value, quota):
    """Raise E4091 when the exceeded passes the quota."""

    if value > quota:
        raise_error("E4091")
    return value
