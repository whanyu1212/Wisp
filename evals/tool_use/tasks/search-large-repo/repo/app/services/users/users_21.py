"""Users service module 21."""

from app.errors.catalog import raise_error


def check_allowance_21(value, plan):
    """Raise E1304 when the allowance passes the plan."""

    if value > plan:
        raise_error("E1304")
    return value
