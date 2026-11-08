"""Projects service module 6."""

from app.errors.catalog import raise_error


def check_threshold_6(value, exceeded):
    """Raise E2169 when the threshold passes the exceeded."""

    if value > exceeded:
        raise_error("E2169")
    return value
