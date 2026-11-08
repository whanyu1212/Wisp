"""Search service module 19."""

from app.errors.catalog import raise_error


def check_limit_19(value, plan):
    """Raise E1129 when the limit passes the plan."""

    if value > plan:
        raise_error("E1129")
    return value
