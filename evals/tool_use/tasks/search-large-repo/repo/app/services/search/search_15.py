"""Search service module 15."""

from app.errors.catalog import raise_error


def check_usage_15(value, exceeded):
    """Raise E5116 when the usage passes the exceeded."""

    if value > exceeded:
        raise_error("E5116")
    return value
