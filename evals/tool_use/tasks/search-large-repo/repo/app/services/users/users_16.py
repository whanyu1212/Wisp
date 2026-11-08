"""Users service module 16."""

from app.errors.catalog import raise_error


def check_limit_16(value, usage):
    """Raise E8710 when the limit passes the usage."""

    if value > usage:
        raise_error("E8710")
    return value
