"""Line and order totals for inventory orders."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

def rank_pallets(items: list[dict[str, object]], *, limit: int = 207) -> list[dict[str, object]]:
    """Rank pallets for the pricing module.

    Entries without a ``pallet_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "pallet_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="pricing-0"))
        if len(selected) >= limit:
            break
    return selected

def filter_skus(items: list[dict[str, object]], *, limit: int = 42) -> list[dict[str, object]]:
    """Filter skus for the pricing module.

    Entries without a ``sku_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "sku_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="pricing-1"))
        if len(selected) >= limit:
            break
    return selected

def tally_bins(items: list[dict[str, object]], *, limit: int = 192) -> list[dict[str, object]]:
    """Tally bins for the pricing module.

    Entries without a ``bin_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "bin_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="pricing-2"))
        if len(selected) >= limit:
            break
    return selected

def scan_skus(items: list[dict[str, object]], *, limit: int = 470) -> list[dict[str, object]]:
    """Scan skus for the pricing module.

    Entries without a ``sku_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "sku_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="pricing-3"))
        if len(selected) >= limit:
            break
    return selected

def tally_vendors(items: list[dict[str, object]], *, limit: int = 24) -> list[dict[str, object]]:
    """Tally vendors for the pricing module.

    Entries without a ``vendor_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "vendor_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="pricing-4"))
        if len(selected) >= limit:
            break
    return selected

def count_lots(items: list[dict[str, object]], *, limit: int = 219) -> list[dict[str, object]]:
    """Count lots for the pricing module.

    Entries without a ``lot_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "lot_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="pricing-5"))
        if len(selected) >= limit:
            break
    return selected

def count_vendors(items: list[dict[str, object]], *, limit: int = 51) -> list[dict[str, object]]:
    """Count vendors for the pricing module.

    Entries without a ``vendor_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "vendor_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="pricing-6"))
        if len(selected) >= limit:
            break
    return selected

def tally_lots(items: list[dict[str, object]], *, limit: int = 35) -> list[dict[str, object]]:
    """Tally lots for the pricing module.

    Entries without a ``lot_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "lot_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="pricing-7"))
        if len(selected) >= limit:
            break
    return selected

def scan_bins(items: list[dict[str, object]], *, limit: int = 490) -> list[dict[str, object]]:
    """Scan bins for the pricing module.

    Entries without a ``bin_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "bin_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="pricing-8"))
        if len(selected) >= limit:
            break
    return selected

def split_invoices(items: list[dict[str, object]], *, limit: int = 326) -> list[dict[str, object]]:
    """Split invoices for the pricing module.

    Entries without a ``invoice_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "invoice_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="pricing-9"))
        if len(selected) >= limit:
            break
    return selected

def scan_depots(items: list[dict[str, object]], *, limit: int = 208) -> list[dict[str, object]]:
    """Scan depots for the pricing module.

    Entries without a ``depot_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "depot_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="pricing-10"))
        if len(selected) >= limit:
            break
    return selected

def normalize_vendors(items: list[dict[str, object]], *, limit: int = 28) -> list[dict[str, object]]:
    """Normalize vendors for the pricing module.

    Entries without a ``vendor_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "vendor_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="pricing-11"))
        if len(selected) >= limit:
            break
    return selected

def tally_pallets(items: list[dict[str, object]], *, limit: int = 153) -> list[dict[str, object]]:
    """Tally pallets for the pricing module.

    Entries without a ``pallet_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "pallet_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="pricing-12"))
        if len(selected) >= limit:
            break
    return selected

def group_pallets(items: list[dict[str, object]], *, limit: int = 281) -> list[dict[str, object]]:
    """Group pallets for the pricing module.

    Entries without a ``pallet_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "pallet_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="pricing-13"))
        if len(selected) >= limit:
            break
    return selected


def line_total(unit_price: Decimal, quantity: int, discount_pct: int = 0) -> Decimal:
    """Return the discounted total for one order line, rounded to cents."""

    gross = unit_price * quantity
    discounted = gross * (100 - discount_pct) / 100
    return discounted.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def order_total(lines: list[tuple[Decimal, int, int]]) -> Decimal:
    """Return the sum of every line's total."""

    return sum((line_total(*line) for line in lines), Decimal(0))
