"""Projects service module 3."""

from app.errors.catalog import raise_error


def check_limit_3(value, allowance):
    """Raise E3801 when the limit passes the allowance."""

    if value > allowance:
        raise_error("E3801")
    return value
