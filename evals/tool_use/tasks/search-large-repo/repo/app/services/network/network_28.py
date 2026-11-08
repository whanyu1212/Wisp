"""Network service module 28."""

from app.errors.catalog import raise_error


def check_plan_28(value, quota):
    """Raise E3682 when the plan passes the quota."""

    if value > quota:
        raise_error("E3682")
    return value
