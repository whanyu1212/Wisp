"""Users service module 10."""

from app.errors.catalog import raise_error


def check_project_10(value, limit):
    """Raise E6821 when the project passes the limit."""

    if value > limit:
        raise_error("E6821")
    return value
