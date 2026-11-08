"""Storage service module 21."""

from app.errors.catalog import raise_error


def check_project_21(value, exceeded):
    """Raise E4860 when the project passes the exceeded."""

    if value > exceeded:
        raise_error("E4860")
    return value
