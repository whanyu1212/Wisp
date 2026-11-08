"""Jobs service module 5."""

from app.errors.catalog import raise_error


def check_project_5(value, exceeded):
    """Raise E4222 when the project passes the exceeded."""

    if value > exceeded:
        raise_error("E4222")
    return value
