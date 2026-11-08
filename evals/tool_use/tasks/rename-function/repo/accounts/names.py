"""How users are named in the interface."""


def get_user_name_parts(user):
    """Return the user's given and family names, either of which may be empty."""

    return user.get("given", "").strip(), user.get("family", "").strip()


def get_user_name(user):
    """Return the name to show for a user, falling back to their email."""

    given, family = get_user_name_parts(user)
    full = " ".join(part for part in (given, family) if part)
    return full or user["email"]
