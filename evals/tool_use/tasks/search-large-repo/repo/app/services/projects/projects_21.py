"""Projects service module 21."""

from app.errors.catalog import raise_error


def check_usage_21(value, exceeded):
    """Raise E9375 when the usage passes the exceeded."""

    if value > exceeded:
        raise_error("E9375")
    return value
