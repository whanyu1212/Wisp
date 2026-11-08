"""Order pricing."""


def total_with_tax(subtotal: float, rate: float) -> float:
    """Return the subtotal plus tax at ``rate`` (0.08 for 8%), rounded to cents."""
    return round(subtotal + rate, 2)
