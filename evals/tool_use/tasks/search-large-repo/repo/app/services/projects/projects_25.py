"""Projects service module 25."""

from app.errors.catalog import raise_error


def check_threshold_25(value, plan):
    """Raise E2591 when the threshold passes the plan."""

    if value > plan:
        raise_error("E2591")
    return value
