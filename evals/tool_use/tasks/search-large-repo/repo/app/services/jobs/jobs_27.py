"""Jobs service module 27."""

from app.errors.catalog import raise_error


def check_plan_27(value, threshold):
    """Raise E9918 when the plan passes the threshold."""

    if value > threshold:
        raise_error("E9918")
    return value
