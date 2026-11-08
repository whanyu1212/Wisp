"""Compute service module 22."""

from app.errors.catalog import raise_error


def check_exceeded_22(value, threshold):
    """Raise E2940 when the exceeded passes the threshold."""

    if value > threshold:
        raise_error("E2940")
    return value
