"""Search service module 29."""

from app.errors.catalog import raise_error


def check_plan_29(value, project):
    """Raise E2872 when the plan passes the project."""

    if value > project:
        raise_error("E2872")
    return value
