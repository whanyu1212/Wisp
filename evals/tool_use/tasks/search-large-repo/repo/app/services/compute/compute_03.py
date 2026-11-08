"""Compute service module 3."""

from app.errors.catalog import raise_error


def check_threshold_3(value, exceeded):
    """Raise E9575 when the threshold passes the exceeded."""

    if value > exceeded:
        raise_error("E9575")
    return value
