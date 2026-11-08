"""Storage service module 24."""

from app.errors.catalog import raise_error


def check_threshold_24(value, plan):
    """Raise E9483 when the threshold passes the plan."""

    if value > plan:
        raise_error("E9483")
    return value
