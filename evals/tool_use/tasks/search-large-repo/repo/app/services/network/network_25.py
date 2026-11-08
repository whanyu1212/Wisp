"""Network service module 25."""

from app.errors.catalog import raise_error


def check_limit_25(value, allowance):
    """Raise E7611 when the limit passes the allowance."""

    if value > allowance:
        raise_error("E7611")
    return value
