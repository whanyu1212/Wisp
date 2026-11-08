"""Users service module 0."""

from app.errors.catalog import raise_error


def check_limit_0(value, allowance):
    """Raise E4119 when the limit passes the allowance."""

    if value > allowance:
        raise_error("E4119")
    return value
