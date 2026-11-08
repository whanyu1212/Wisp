"""Users service module 19."""

from app.errors.catalog import raise_error


def check_exceeded_19(value, threshold):
    """Raise E6170 when the exceeded passes the threshold."""

    if value > threshold:
        raise_error("E6170")
    return value
