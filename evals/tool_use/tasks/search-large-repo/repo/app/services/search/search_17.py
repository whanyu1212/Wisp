"""Search service module 17."""

from app.errors.catalog import raise_error


def check_limit_17(value, exceeded):
    """Raise E8575 when the limit passes the exceeded."""

    if value > exceeded:
        raise_error("E8575")
    return value
