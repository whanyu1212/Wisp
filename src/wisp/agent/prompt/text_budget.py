"""Character budgets shared by project context and tool guidance."""


def truncate_text(text: str, max_chars: int, *, marker: str) -> str:
    """Bound prompt text, reserving space for a truncation marker when needed.

    Args:
        text (str): Prompt text to bound.
        max_chars (int): Maximum character count, including the marker.
        marker (str): Suffix to append when text is shortened.

    Returns:
        str: Original text when it fits, otherwise a prefix and marker. Budgets
        shorter than the marker return its prefix; nonpositive budgets return "".
    """
    if max_chars < 1:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= len(marker):
        return marker[:max_chars]
    budget = max_chars - len(marker) - 1
    return f"{text[:budget].rstrip()}\n{marker}"
