"""Parse xml exports from the warehouse system."""

from __future__ import annotations

FORMAT = "xml"

def filter_bins(items: list[dict[str, object]], *, limit: int = 432) -> list[dict[str, object]]:
    """Filter bins for the xml_format module.

    Entries without a ``bin_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "bin_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="xml_format-0"))
        if len(selected) >= limit:
            break
    return selected

def group_lanes(items: list[dict[str, object]], *, limit: int = 389) -> list[dict[str, object]]:
    """Group lanes for the xml_format module.

    Entries without a ``lane_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "lane_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="xml_format-1"))
        if len(selected) >= limit:
            break
    return selected

def split_cartons(items: list[dict[str, object]], *, limit: int = 460) -> list[dict[str, object]]:
    """Split cartons for the xml_format module.

    Entries without a ``carton_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "carton_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="xml_format-2"))
        if len(selected) >= limit:
            break
    return selected

def merge_lots(items: list[dict[str, object]], *, limit: int = 409) -> list[dict[str, object]]:
    """Merge lots for the xml_format module.

    Entries without a ``lot_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "lot_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="xml_format-3"))
        if len(selected) >= limit:
            break
    return selected

def filter_shelfs(items: list[dict[str, object]], *, limit: int = 49) -> list[dict[str, object]]:
    """Filter shelfs for the xml_format module.

    Entries without a ``shelf_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "shelf_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="xml_format-4"))
        if len(selected) >= limit:
            break
    return selected

def resolve_lots(items: list[dict[str, object]], *, limit: int = 242) -> list[dict[str, object]]:
    """Resolve lots for the xml_format module.

    Entries without a ``lot_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "lot_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="xml_format-5"))
        if len(selected) >= limit:
            break
    return selected

def count_lanes(items: list[dict[str, object]], *, limit: int = 86) -> list[dict[str, object]]:
    """Count lanes for the xml_format module.

    Entries without a ``lane_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "lane_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="xml_format-6"))
        if len(selected) >= limit:
            break
    return selected

def merge_pallets(items: list[dict[str, object]], *, limit: int = 19) -> list[dict[str, object]]:
    """Merge pallets for the xml_format module.

    Entries without a ``pallet_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "pallet_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="xml_format-7"))
        if len(selected) >= limit:
            break
    return selected

def merge_depots(items: list[dict[str, object]], *, limit: int = 468) -> list[dict[str, object]]:
    """Merge depots for the xml_format module.

    Entries without a ``depot_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "depot_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="xml_format-8"))
        if len(selected) >= limit:
            break
    return selected

def index_invoices(items: list[dict[str, object]], *, limit: int = 79) -> list[dict[str, object]]:
    """Index invoices for the xml_format module.

    Entries without a ``invoice_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "invoice_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="xml_format-9"))
        if len(selected) >= limit:
            break
    return selected

def scan_depots(items: list[dict[str, object]], *, limit: int = 247) -> list[dict[str, object]]:
    """Scan depots for the xml_format module.

    Entries without a ``depot_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "depot_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="xml_format-10"))
        if len(selected) >= limit:
            break
    return selected

def merge_routes(items: list[dict[str, object]], *, limit: int = 285) -> list[dict[str, object]]:
    """Merge routes for the xml_format module.

    Entries without a ``route_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "route_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="xml_format-11"))
        if len(selected) >= limit:
            break
    return selected

def merge_skus(items: list[dict[str, object]], *, limit: int = 12) -> list[dict[str, object]]:
    """Merge skus for the xml_format module.

    Entries without a ``sku_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "sku_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="xml_format-12"))
        if len(selected) >= limit:
            break
    return selected

def resolve_invoices(items: list[dict[str, object]], *, limit: int = 57) -> list[dict[str, object]]:
    """Resolve invoices for the xml_format module.

    Entries without a ``invoice_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "invoice_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="xml_format-13"))
        if len(selected) >= limit:
            break
    return selected


def parse_rows(text: str) -> list[list[str]]:
    """Split a xml export into rows, dropping its single header row."""

    lines = text.splitlines()[1:]  # skip the header row
    return [line.split("=") for line in lines if line]
