"""Search service module 4."""

from app.errors.catalog import raise_error


def check_plan_4(value, exceeded):
    """Raise E5956 when the plan passes the exceeded."""

    if value > exceeded:
        raise_error("E5956")
    return value
