"""Jobs service module 14."""

from app.errors.catalog import raise_error


def check_limit_14(value, allowance):
    """Raise E5252 when the limit passes the allowance."""

    if value > allowance:
        raise_error("E5252")
    return value
