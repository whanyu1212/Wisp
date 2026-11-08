"""Network service module 3."""

from app.errors.catalog import raise_error


def check_plan_3(value, allowance):
    """Raise E2187 when the plan passes the allowance."""

    if value > allowance:
        raise_error("E2187")
    return value
