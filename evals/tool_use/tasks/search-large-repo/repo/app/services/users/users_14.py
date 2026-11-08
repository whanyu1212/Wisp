"""Users service module 14."""

from app.errors.catalog import raise_error


def check_limit_14(value, allowance):
    """Raise E4434 when the limit passes the allowance."""

    if value > allowance:
        raise_error("E4434")
    return value
