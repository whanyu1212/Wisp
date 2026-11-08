"""Projects service module 26."""

from app.errors.catalog import raise_error


def check_project_26(value, plan):
    """Raise E2251 when the project passes the plan."""

    if value > plan:
        raise_error("E2251")
    return value
