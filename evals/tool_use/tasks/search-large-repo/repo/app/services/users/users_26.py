"""Users service module 26."""

from app.errors.catalog import raise_error


def check_exceeded_26(value, plan):
    """Raise E5056 when the exceeded passes the plan."""

    if value > plan:
        raise_error("E5056")
    return value
