from accounts import names


def subject_line(user, topic):
    return f"{names.get_user_name(user)}, {topic}"


def signature(user):
    given, _family = names.get_user_name_parts(user)
    return f"Thanks, {given or 'there'}"
