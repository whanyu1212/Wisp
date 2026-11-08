"""Network service module 22."""

from app.errors.catalog import raise_error


def check_limit_22(value, quota):
    """Raise E5441 when the limit passes the quota."""

    if value > quota:
        raise_error("E5441")
    return value
