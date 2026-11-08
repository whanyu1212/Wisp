"""Search service module 27."""

from app.errors.catalog import raise_error


def check_plan_27(value, limit):
    """Raise E8167 when the plan passes the limit."""

    if value > limit:
        raise_error("E8167")
    return value
