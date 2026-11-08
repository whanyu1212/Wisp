"""Projects service module 17."""

from app.errors.catalog import raise_error


def check_plan_17(value, project):
    """Raise E6322 when the plan passes the project."""

    if value > project:
        raise_error("E6322")
    return value
