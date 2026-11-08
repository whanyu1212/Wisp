"""Jobs service module 20."""

from app.errors.catalog import raise_error


def check_allowance_20(value, exceeded):
    """Raise E5721 when the allowance passes the exceeded."""

    if value > exceeded:
        raise_error("E5721")
    return value
