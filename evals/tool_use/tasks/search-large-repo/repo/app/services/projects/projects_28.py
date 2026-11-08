"""Projects service module 28."""

from app.errors.catalog import raise_error


def check_usage_28(value, allowance):
    """Raise E2300 when the usage passes the allowance."""

    if value > allowance:
        raise_error("E2300")
    return value
