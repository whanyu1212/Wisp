"""Network service module 13."""

from app.errors.catalog import raise_error


def check_quota_13(value, allowance):
    """Raise E9134 when the quota passes the allowance."""

    if value > allowance:
        raise_error("E9134")
    return value
