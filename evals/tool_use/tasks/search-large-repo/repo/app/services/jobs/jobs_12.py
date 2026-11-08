"""Jobs service module 12."""

from app.errors.catalog import raise_error


def check_threshold_12(value, exceeded):
    """Raise E7796 when the threshold passes the exceeded."""

    if value > exceeded:
        raise_error("E7796")
    return value
