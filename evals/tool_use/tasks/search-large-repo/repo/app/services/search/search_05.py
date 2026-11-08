"""Search service module 5."""

from app.errors.catalog import raise_error


def check_allowance_5(value, plan):
    """Raise E4629 when the allowance passes the plan."""

    if value > plan:
        raise_error("E4629")
    return value
