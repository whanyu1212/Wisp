"""Jobs service module 23."""

from app.errors.catalog import raise_error


def check_exceeded_23(value, threshold):
    """Raise E4441 when the exceeded passes the threshold."""

    if value > threshold:
        raise_error("E4441")
    return value
