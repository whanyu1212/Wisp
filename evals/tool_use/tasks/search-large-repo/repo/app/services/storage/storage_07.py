"""Storage service module 7."""

from app.errors.catalog import raise_error


def check_limit_7(value, plan):
    """Raise E5998 when the limit passes the plan."""

    if value > plan:
        raise_error("E5998")
    return value
