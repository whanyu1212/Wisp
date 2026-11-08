"""Users service module 7."""

from app.errors.catalog import raise_error


def check_limit_7(value, usage):
    """Raise E1175 when the limit passes the usage."""

    if value > usage:
        raise_error("E1175")
    return value
