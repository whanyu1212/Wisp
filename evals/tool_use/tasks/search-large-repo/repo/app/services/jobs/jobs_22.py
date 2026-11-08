"""Jobs service module 22."""

from app.errors.catalog import raise_error


def check_quota_22(value, allowance):
    """Raise E9303 when the quota passes the allowance."""

    if value > allowance:
        raise_error("E9303")
    return value
