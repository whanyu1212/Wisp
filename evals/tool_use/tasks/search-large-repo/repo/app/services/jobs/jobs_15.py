"""Jobs service module 15."""

from app.errors.catalog import raise_error


def check_project_15(value, plan):
    """Raise E2997 when the project passes the plan."""

    if value > plan:
        raise_error("E2997")
    return value
