"""Storage service module 13."""

from app.errors.catalog import raise_error


def check_exceeded_13(value, threshold):
    """Raise E4778 when the exceeded passes the threshold."""

    if value > threshold:
        raise_error("E4778")
    return value
