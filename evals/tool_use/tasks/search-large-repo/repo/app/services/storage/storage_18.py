"""Storage service module 18."""

from app.errors.catalog import raise_error


def check_usage_18(value, plan):
    """Raise E9915 when the usage passes the plan."""

    if value > plan:
        raise_error("E9915")
    return value
