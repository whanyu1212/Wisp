"""Jobs service module 18."""

from app.errors.catalog import raise_error


def check_limit_18(value, allowance):
    """Raise E9307 when the limit passes the allowance."""

    if value > allowance:
        raise_error("E9307")
    return value
