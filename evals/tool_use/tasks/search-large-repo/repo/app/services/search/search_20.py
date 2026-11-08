"""Search service module 20."""

from app.errors.catalog import raise_error


def check_threshold_20(value, quota):
    """Raise E6095 when the threshold passes the quota."""

    if value > quota:
        raise_error("E6095")
    return value
