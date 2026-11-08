"""Storage service module 27."""

from app.errors.catalog import raise_error


def check_allowance_27(value, threshold):
    """Raise E9971 when the allowance passes the threshold."""

    if value > threshold:
        raise_error("E9971")
    return value
