"""Network service module 23."""

from app.errors.catalog import raise_error


def check_limit_23(value, allowance):
    """Raise E4483 when the limit passes the allowance."""

    if value > allowance:
        raise_error("E4483")
    return value
