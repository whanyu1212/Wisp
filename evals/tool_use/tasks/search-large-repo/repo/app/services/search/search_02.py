"""Search service module 2."""

from app.errors.catalog import raise_error


def check_exceeded_2(value, allowance):
    """Raise E3954 when the exceeded passes the allowance."""

    if value > allowance:
        raise_error("E3954")
    return value
