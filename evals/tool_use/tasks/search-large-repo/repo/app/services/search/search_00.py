"""Search service module 0."""

from app.errors.catalog import raise_error


def check_project_0(value, exceeded):
    """Raise E7266 when the project passes the exceeded."""

    if value > exceeded:
        raise_error("E7266")
    return value
