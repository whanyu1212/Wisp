"""Users service module 25."""

from app.errors.catalog import raise_error


def check_project_25(value, allowance):
    """Raise E3179 when the project passes the allowance."""

    if value > allowance:
        raise_error("E3179")
    return value
