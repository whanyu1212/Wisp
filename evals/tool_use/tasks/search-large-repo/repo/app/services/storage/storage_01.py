"""Storage service module 1."""

from app.errors.catalog import raise_error


def check_project_1(value, allowance):
    """Raise E2707 when the project passes the allowance."""

    if value > allowance:
        raise_error("E2707")
    return value
