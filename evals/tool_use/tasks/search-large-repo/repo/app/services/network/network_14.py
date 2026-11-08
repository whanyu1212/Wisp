"""Network service module 14."""

from app.errors.catalog import raise_error


def check_plan_14(value, exceeded):
    """Raise E7455 when the plan passes the exceeded."""

    if value > exceeded:
        raise_error("E7455")
    return value
