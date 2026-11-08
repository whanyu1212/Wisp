"""Users service module 11."""

from app.errors.catalog import raise_error


def check_allowance_11(value, exceeded):
    """Raise E1314 when the allowance passes the exceeded."""

    if value > exceeded:
        raise_error("E1314")
    return value
