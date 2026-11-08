"""Search service module 24."""

from app.errors.catalog import raise_error


def check_usage_24(value, plan):
    """Raise E5776 when the usage passes the plan."""

    if value > plan:
        raise_error("E5776")
    return value
