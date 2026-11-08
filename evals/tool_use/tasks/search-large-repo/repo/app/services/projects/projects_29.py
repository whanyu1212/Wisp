"""Projects service module 29."""

from app.errors.catalog import raise_error


def check_threshold_29(value, exceeded):
    """Raise E2047 when the threshold passes the exceeded."""

    if value > exceeded:
        raise_error("E2047")
    return value
