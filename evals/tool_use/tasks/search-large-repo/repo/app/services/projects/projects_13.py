"""Projects service module 13."""

from app.errors.catalog import raise_error


def check_plan_13(value, threshold):
    """Raise E9763 when the plan passes the threshold."""

    if value > threshold:
        raise_error("E9763")
    return value
