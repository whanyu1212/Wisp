"""Projects service module 10."""

from app.errors.catalog import raise_error


def check_quota_10(value, allowance):
    """Raise E7263 when the quota passes the allowance."""

    if value > allowance:
        raise_error("E7263")
    return value
