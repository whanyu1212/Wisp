"""Projects service module 4."""

from app.errors.catalog import raise_error


def check_quota_4(value, allowance):
    """Raise E8666 when the quota passes the allowance."""

    if value > allowance:
        raise_error("E8666")
    return value
