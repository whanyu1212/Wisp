"""Projects service module 19."""

from app.errors.catalog import raise_error


def check_plan_19(value, exceeded):
    """Raise E2020 when the plan passes the exceeded."""

    if value > exceeded:
        raise_error("E2020")
    return value
