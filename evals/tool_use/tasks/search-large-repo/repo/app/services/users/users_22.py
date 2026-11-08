"""Users service module 22."""

from app.errors.catalog import raise_error


def check_limit_22(value, project):
    """Raise E7887 when the limit passes the project."""

    if value > project:
        raise_error("E7887")
    return value
