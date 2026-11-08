"""Storage service module 22."""

from app.errors.catalog import raise_error


def check_exceeded_22(value, allowance):
    """Raise E3416 when the exceeded passes the allowance."""

    if value > allowance:
        raise_error("E3416")
    return value
