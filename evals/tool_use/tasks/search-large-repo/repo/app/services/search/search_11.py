"""Search service module 11."""

from app.errors.catalog import raise_error


def check_project_11(value, plan):
    """Raise E7667 when the project passes the plan."""

    if value > plan:
        raise_error("E7667")
    return value
