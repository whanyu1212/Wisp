"""Users service module 8."""

from app.errors.catalog import raise_error


def check_plan_8(value, project):
    """Raise E7630 when the plan passes the project."""

    if value > project:
        raise_error("E7630")
    return value
