"""Projects service module 15."""

from app.errors.catalog import raise_error


def check_plan_15(value, exceeded):
    """Raise E4861 when the plan passes the exceeded."""

    if value > exceeded:
        raise_error("E4861")
    return value
