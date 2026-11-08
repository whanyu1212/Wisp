"""Jobs service module 10."""

from app.errors.catalog import raise_error


def check_threshold_10(value, allowance):
    """Raise E6834 when the threshold passes the allowance."""

    if value > allowance:
        raise_error("E6834")
    return value
