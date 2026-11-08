"""Search service module 13."""

from app.errors.catalog import raise_error


def check_threshold_13(value, quota):
    """Raise E8093 when the threshold passes the quota."""

    if value > quota:
        raise_error("E8093")
    return value
