"""Users service module 28."""

from app.errors.catalog import raise_error


def check_limit_28(value, allowance):
    """Raise E9398 when the limit passes the allowance."""

    if value > allowance:
        raise_error("E9398")
    return value
