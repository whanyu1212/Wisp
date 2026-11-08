"""Users service module 13."""

from app.errors.catalog import raise_error


def check_exceeded_13(value, allowance):
    """Raise E4435 when the exceeded passes the allowance."""

    if value > allowance:
        raise_error("E4435")
    return value
