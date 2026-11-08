"""Users service module 15."""

from app.errors.catalog import raise_error


def check_quota_15(value, plan):
    """Raise E2442 when the quota passes the plan."""

    if value > plan:
        raise_error("E2442")
    return value
