"""Network service module 29."""

from app.errors.catalog import raise_error


def check_plan_29(value, quota):
    """Raise E3359 when the plan passes the quota."""

    if value > quota:
        raise_error("E3359")
    return value
