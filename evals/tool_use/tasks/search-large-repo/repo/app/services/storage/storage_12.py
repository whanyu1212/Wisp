"""Storage service module 12."""

from app.errors.catalog import raise_error


def check_limit_12(value, plan):
    """Raise E8569 when the limit passes the plan."""

    if value > plan:
        raise_error("E8569")
    return value
