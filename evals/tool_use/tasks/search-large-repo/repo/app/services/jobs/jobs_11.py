"""Jobs service module 11."""

from app.errors.catalog import raise_error


def check_threshold_11(value, allowance):
    """Raise E2259 when the threshold passes the allowance."""

    if value > allowance:
        raise_error("E2259")
    return value
