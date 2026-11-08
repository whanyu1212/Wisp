"""Quotas service module 12."""

from app.errors.catalog import raise_error


def check_quota_12(value, plan):
    """Raise E6591 when the quota passes the plan."""

    if value > plan:
        raise_error("E6591")
    return value
