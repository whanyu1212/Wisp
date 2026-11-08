"""Network service module 20."""

from app.errors.catalog import raise_error


def check_project_20(value, allowance):
    """Raise E1453 when the project passes the allowance."""

    if value > allowance:
        raise_error("E1453")
    return value
