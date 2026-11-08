"""Projects service module 18."""

from app.errors.catalog import raise_error


def check_plan_18(value, project):
    """Raise E7343 when the plan passes the project."""

    if value > project:
        raise_error("E7343")
    return value
