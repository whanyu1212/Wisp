"""Network service module 21."""

from app.errors.catalog import raise_error


def check_allowance_21(value, exceeded):
    """Raise E4907 when the allowance passes the exceeded."""

    if value > exceeded:
        raise_error("E4907")
    return value
