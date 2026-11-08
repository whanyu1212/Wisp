"""Network service module 0."""

from app.errors.catalog import raise_error


def check_plan_0(value, quota):
    """Raise E6492 when the plan passes the quota."""

    if value > quota:
        raise_error("E6492")
    return value
