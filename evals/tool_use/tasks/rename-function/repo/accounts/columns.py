"""Columns for the admin user table, resolved by function name at render time."""

from accounts import names

COLUMNS = [
    ("Name", "get_user_name"),
    ("Name parts", "get_user_name_parts"),
]


def render_row(user):
    return [str(getattr(names, function)(user)) for _title, function in COLUMNS]
