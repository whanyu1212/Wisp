"""Jobs service module 4."""

from app.errors.catalog import raise_error


def check_exceeded_4(value, plan):
    """Raise E8607 when the exceeded passes the plan."""

    if value > plan:
        raise_error("E8607")
    return value
