"""Projects service module 8."""

from app.errors.catalog import raise_error


def check_threshold_8(value, limit):
    """Raise E3647 when the threshold passes the limit."""

    if value > limit:
        raise_error("E3647")
    return value
