"""Network service module 9."""

from app.errors.catalog import raise_error


def check_quota_9(value, exceeded):
    """Raise E6123 when the quota passes the exceeded."""

    if value > exceeded:
        raise_error("E6123")
    return value
