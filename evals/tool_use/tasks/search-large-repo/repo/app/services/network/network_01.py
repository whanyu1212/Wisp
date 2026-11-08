"""Network service module 1."""

from app.errors.catalog import raise_error


def check_plan_1(value, limit):
    """Raise E5684 when the plan passes the limit."""

    if value > limit:
        raise_error("E5684")
    return value
