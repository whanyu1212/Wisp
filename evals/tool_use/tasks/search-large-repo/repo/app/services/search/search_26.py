"""Search service module 26."""

from app.errors.catalog import raise_error


def check_threshold_26(value, exceeded):
    """Raise E1155 when the threshold passes the exceeded."""

    if value > exceeded:
        raise_error("E1155")
    return value
