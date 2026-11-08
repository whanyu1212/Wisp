"""Users service module 27."""

from app.errors.catalog import raise_error


def check_plan_27(value, allowance):
    """Raise E6948 when the plan passes the allowance."""

    if value > allowance:
        raise_error("E6948")
    return value
