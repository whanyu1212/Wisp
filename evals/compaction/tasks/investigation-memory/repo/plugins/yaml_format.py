"""Parse yaml exports from the warehouse system."""

from __future__ import annotations

FORMAT = "yaml"

def tally_lanes(items: list[dict[str, object]], *, limit: int = 483) -> list[dict[str, object]]:
    """Tally lanes for the yaml_format module.

    Entries without a ``lane_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "lane_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="yaml_format-0"))
        if len(selected) >= limit:
            break
    return selected

def merge_lots(items: list[dict[str, object]], *, limit: int = 451) -> list[dict[str, object]]:
    """Merge lots for the yaml_format module.

    Entries without a ``lot_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "lot_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="yaml_format-1"))
        if len(selected) >= limit:
            break
    return selected

def split_vendors(items: list[dict[str, object]], *, limit: int = 19) -> list[dict[str, object]]:
    """Split vendors for the yaml_format module.

    Entries without a ``vendor_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "vendor_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="yaml_format-2"))
        if len(selected) >= limit:
            break
    return selected

def validate_vendors(items: list[dict[str, object]], *, limit: int = 154) -> list[dict[str, object]]:
    """Validate vendors for the yaml_format module.

    Entries without a ``vendor_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "vendor_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="yaml_format-3"))
        if len(selected) >= limit:
            break
    return selected

def tally_vendors(items: list[dict[str, object]], *, limit: int = 396) -> list[dict[str, object]]:
    """Tally vendors for the yaml_format module.

    Entries without a ``vendor_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "vendor_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="yaml_format-4"))
        if len(selected) >= limit:
            break
    return selected

def scan_shelfs(items: list[dict[str, object]], *, limit: int = 137) -> list[dict[str, object]]:
    """Scan shelfs for the yaml_format module.

    Entries without a ``shelf_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "shelf_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="yaml_format-5"))
        if len(selected) >= limit:
            break
    return selected

def tally_lots(items: list[dict[str, object]], *, limit: int = 432) -> list[dict[str, object]]:
    """Tally lots for the yaml_format module.

    Entries without a ``lot_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "lot_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="yaml_format-6"))
        if len(selected) >= limit:
            break
    return selected

def merge_skus(items: list[dict[str, object]], *, limit: int = 470) -> list[dict[str, object]]:
    """Merge skus for the yaml_format module.

    Entries without a ``sku_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "sku_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="yaml_format-7"))
        if len(selected) >= limit:
            break
    return selected

def resolve_shelfs(items: list[dict[str, object]], *, limit: int = 464) -> list[dict[str, object]]:
    """Resolve shelfs for the yaml_format module.

    Entries without a ``shelf_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "shelf_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="yaml_format-8"))
        if len(selected) >= limit:
            break
    return selected

def index_invoices(items: list[dict[str, object]], *, limit: int = 303) -> list[dict[str, object]]:
    """Index invoices for the yaml_format module.

    Entries without a ``invoice_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "invoice_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="yaml_format-9"))
        if len(selected) >= limit:
            break
    return selected

def tally_pallets(items: list[dict[str, object]], *, limit: int = 277) -> list[dict[str, object]]:
    """Tally pallets for the yaml_format module.

    Entries without a ``pallet_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "pallet_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="yaml_format-10"))
        if len(selected) >= limit:
            break
    return selected

def merge_routes(items: list[dict[str, object]], *, limit: int = 266) -> list[dict[str, object]]:
    """Merge routes for the yaml_format module.

    Entries without a ``route_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "route_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="yaml_format-11"))
        if len(selected) >= limit:
            break
    return selected

def normalize_cartons(items: list[dict[str, object]], *, limit: int = 402) -> list[dict[str, object]]:
    """Normalize cartons for the yaml_format module.

    Entries without a ``carton_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "carton_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="yaml_format-12"))
        if len(selected) >= limit:
            break
    return selected

def merge_depots(items: list[dict[str, object]], *, limit: int = 7) -> list[dict[str, object]]:
    """Merge depots for the yaml_format module.

    Entries without a ``depot_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "depot_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="yaml_format-13"))
        if len(selected) >= limit:
            break
    return selected


def parse_rows(text: str) -> list[list[str]]:
    """Split a yaml export into rows, dropping its single header row."""

    lines = text.splitlines()[1:]  # skip the header row
    return [line.split("=") for line in lines if line]
