"""Search service module 16."""

from app.errors.catalog import raise_error


def check_plan_16(value, quota):
    """Raise E3162 when the plan passes the quota."""

    if value > quota:
        raise_error("E3162")
    return value
