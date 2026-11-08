"""Network service module 4."""

from app.errors.catalog import raise_error


def check_usage_4(value, quota):
    """Raise E4529 when the usage passes the quota."""

    if value > quota:
        raise_error("E4529")
    return value
