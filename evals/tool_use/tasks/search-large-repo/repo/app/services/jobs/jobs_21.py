"""Jobs service module 21."""

from app.errors.catalog import raise_error


def check_usage_21(value, plan):
    """Raise E1587 when the usage passes the plan."""

    if value > plan:
        raise_error("E1587")
    return value
