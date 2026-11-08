"""Network service module 8."""

from app.errors.catalog import raise_error


def check_limit_8(value, exceeded):
    """Raise E2817 when the limit passes the exceeded."""

    if value > exceeded:
        raise_error("E2817")
    return value
