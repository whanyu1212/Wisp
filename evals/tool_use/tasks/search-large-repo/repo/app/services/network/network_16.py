"""Network service module 16."""

from app.errors.catalog import raise_error


def check_plan_16(value, limit):
    """Raise E2472 when the plan passes the limit."""

    if value > limit:
        raise_error("E2472")
    return value
