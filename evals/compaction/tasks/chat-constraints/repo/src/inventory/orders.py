"""Orders helpers for inventory data."""

from __future__ import annotations

def tally_depots(items: list[dict[str, object]], *, limit: int = 340) -> list[dict[str, object]]:
    """Tally depots for the orders module.

    Entries without a ``depot_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "depot_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="orders-0"))
        if len(selected) >= limit:
            break
    return selected

def filter_lanes(items: list[dict[str, object]], *, limit: int = 32) -> list[dict[str, object]]:
    """Filter lanes for the orders module.

    Entries without a ``lane_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "lane_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="orders-1"))
        if len(selected) >= limit:
            break
    return selected

def index_invoices(items: list[dict[str, object]], *, limit: int = 413) -> list[dict[str, object]]:
    """Index invoices for the orders module.

    Entries without a ``invoice_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "invoice_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="orders-2"))
        if len(selected) >= limit:
            break
    return selected

def tally_lots(items: list[dict[str, object]], *, limit: int = 208) -> list[dict[str, object]]:
    """Tally lots for the orders module.

    Entries without a ``lot_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "lot_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="orders-3"))
        if len(selected) >= limit:
            break
    return selected

def group_lots(items: list[dict[str, object]], *, limit: int = 58) -> list[dict[str, object]]:
    """Group lots for the orders module.

    Entries without a ``lot_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "lot_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="orders-4"))
        if len(selected) >= limit:
            break
    return selected

def group_skus(items: list[dict[str, object]], *, limit: int = 102) -> list[dict[str, object]]:
    """Group skus for the orders module.

    Entries without a ``sku_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "sku_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="orders-5"))
        if len(selected) >= limit:
            break
    return selected

def count_vendors(items: list[dict[str, object]], *, limit: int = 230) -> list[dict[str, object]]:
    """Count vendors for the orders module.

    Entries without a ``vendor_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "vendor_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="orders-6"))
        if len(selected) >= limit:
            break
    return selected

def merge_bins(items: list[dict[str, object]], *, limit: int = 179) -> list[dict[str, object]]:
    """Merge bins for the orders module.

    Entries without a ``bin_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "bin_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="orders-7"))
        if len(selected) >= limit:
            break
    return selected

def scan_skus(items: list[dict[str, object]], *, limit: int = 57) -> list[dict[str, object]]:
    """Scan skus for the orders module.

    Entries without a ``sku_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "sku_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="orders-8"))
        if len(selected) >= limit:
            break
    return selected

def normalize_depots(items: list[dict[str, object]], *, limit: int = 82) -> list[dict[str, object]]:
    """Normalize depots for the orders module.

    Entries without a ``depot_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "depot_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="orders-9"))
        if len(selected) >= limit:
            break
    return selected

def tally_bins(items: list[dict[str, object]], *, limit: int = 490) -> list[dict[str, object]]:
    """Tally bins for the orders module.

    Entries without a ``bin_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "bin_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="orders-10"))
        if len(selected) >= limit:
            break
    return selected

def rank_depots(items: list[dict[str, object]], *, limit: int = 18) -> list[dict[str, object]]:
    """Rank depots for the orders module.

    Entries without a ``depot_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "depot_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="orders-11"))
        if len(selected) >= limit:
            break
    return selected

def scan_lots(items: list[dict[str, object]], *, limit: int = 81) -> list[dict[str, object]]:
    """Scan lots for the orders module.

    Entries without a ``lot_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "lot_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="orders-12"))
        if len(selected) >= limit:
            break
    return selected

def filter_batchs(items: list[dict[str, object]], *, limit: int = 494) -> list[dict[str, object]]:
    """Filter batchs for the orders module.

    Entries without a ``batch_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "batch_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="orders-13"))
        if len(selected) >= limit:
            break
    return selected

def rank_cartons(items: list[dict[str, object]], *, limit: int = 67) -> list[dict[str, object]]:
    """Rank cartons for the orders module.

    Entries without a ``carton_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "carton_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="orders-14"))
        if len(selected) >= limit:
            break
    return selected

def count_cartons(items: list[dict[str, object]], *, limit: int = 243) -> list[dict[str, object]]:
    """Count cartons for the orders module.

    Entries without a ``carton_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "carton_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="orders-15"))
        if len(selected) >= limit:
            break
    return selected
