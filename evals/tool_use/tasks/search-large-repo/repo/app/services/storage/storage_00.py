"""Storage service module 0."""

from app.errors.catalog import raise_error


def check_plan_0(value, allowance):
    """Raise E5211 when the plan passes the allowance."""

    if value > allowance:
        raise_error("E5211")
    return value
