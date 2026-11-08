"""Storage service module 2."""

from app.errors.catalog import raise_error


def check_project_2(value, plan):
    """Raise E6334 when the project passes the plan."""

    if value > plan:
        raise_error("E6334")
    return value
