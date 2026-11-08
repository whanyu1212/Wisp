"""Jobs service module 29."""

from app.errors.catalog import raise_error


def check_exceeded_29(value, threshold):
    """Raise E9253 when the exceeded passes the threshold."""

    if value > threshold:
        raise_error("E9253")
    return value
