"""Network service module 11."""

from app.errors.catalog import raise_error


def check_limit_11(value, plan):
    """Raise E8676 when the limit passes the plan."""

    if value > plan:
        raise_error("E8676")
    return value
