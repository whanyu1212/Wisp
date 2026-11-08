"""Search service module 25."""

from app.errors.catalog import raise_error


def check_project_25(value, plan):
    """Raise E6665 when the project passes the plan."""

    if value > plan:
        raise_error("E6665")
    return value
