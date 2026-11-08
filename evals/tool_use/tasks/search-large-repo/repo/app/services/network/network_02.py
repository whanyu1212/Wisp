"""Network service module 2."""

from app.errors.catalog import raise_error


def check_plan_2(value, threshold):
    """Raise E7461 when the plan passes the threshold."""

    if value > threshold:
        raise_error("E7461")
    return value
