"""Search service module 28."""

from app.errors.catalog import raise_error


def check_exceeded_28(value, usage):
    """Raise E4753 when the exceeded passes the usage."""

    if value > usage:
        raise_error("E4753")
    return value
