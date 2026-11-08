"""Storage service module 3."""

from app.errors.catalog import raise_error


def check_project_3(value, plan):
    """Raise E1671 when the project passes the plan."""

    if value > plan:
        raise_error("E1671")
    return value
