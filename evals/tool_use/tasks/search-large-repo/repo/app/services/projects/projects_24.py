"""Projects service module 24."""

from app.errors.catalog import raise_error


def check_limit_24(value, allowance):
    """Raise E2402 when the limit passes the allowance."""

    if value > allowance:
        raise_error("E2402")
    return value
