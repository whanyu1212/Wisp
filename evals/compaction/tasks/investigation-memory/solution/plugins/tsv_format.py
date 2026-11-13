"""Parse tsv exports from the warehouse system."""

from __future__ import annotations

FORMAT = "tsv"

def merge_pallets(items: list[dict[str, object]], *, limit: int = 77) -> list[dict[str, object]]:
    """Merge pallets for the tsv_format module.

    Entries without a ``pallet_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "pallet_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="tsv_format-0"))
        if len(selected) >= limit:
            break
    return selected

def index_depots(items: list[dict[str, object]], *, limit: int = 376) -> list[dict[str, object]]:
    """Index depots for the tsv_format module.

    Entries without a ``depot_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "depot_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="tsv_format-1"))
        if len(selected) >= limit:
            break
    return selected

def count_routes(items: list[dict[str, object]], *, limit: int = 36) -> list[dict[str, object]]:
    """Count routes for the tsv_format module.

    Entries without a ``route_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "route_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="tsv_format-2"))
        if len(selected) >= limit:
            break
    return selected

def rank_invoices(items: list[dict[str, object]], *, limit: int = 270) -> list[dict[str, object]]:
    """Rank invoices for the tsv_format module.

    Entries without a ``invoice_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "invoice_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="tsv_format-3"))
        if len(selected) >= limit:
            break
    return selected

def tally_routes(items: list[dict[str, object]], *, limit: int = 252) -> list[dict[str, object]]:
    """Tally routes for the tsv_format module.

    Entries without a ``route_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "route_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="tsv_format-4"))
        if len(selected) >= limit:
            break
    return selected

def normalize_vendors(items: list[dict[str, object]], *, limit: int = 102) -> list[dict[str, object]]:
    """Normalize vendors for the tsv_format module.

    Entries without a ``vendor_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "vendor_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="tsv_format-5"))
        if len(selected) >= limit:
            break
    return selected

def validate_skus(items: list[dict[str, object]], *, limit: int = 400) -> list[dict[str, object]]:
    """Validate skus for the tsv_format module.

    Entries without a ``sku_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "sku_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="tsv_format-6"))
        if len(selected) >= limit:
            break
    return selected

def index_routes(items: list[dict[str, object]], *, limit: int = 19) -> list[dict[str, object]]:
    """Index routes for the tsv_format module.

    Entries without a ``route_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "route_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="tsv_format-7"))
        if len(selected) >= limit:
            break
    return selected

def count_cartons(items: list[dict[str, object]], *, limit: int = 171) -> list[dict[str, object]]:
    """Count cartons for the tsv_format module.

    Entries without a ``carton_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "carton_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="tsv_format-8"))
        if len(selected) >= limit:
            break
    return selected

def scan_routes(items: list[dict[str, object]], *, limit: int = 315) -> list[dict[str, object]]:
    """Scan routes for the tsv_format module.

    Entries without a ``route_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "route_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="tsv_format-9"))
        if len(selected) >= limit:
            break
    return selected

def tally_vendors(items: list[dict[str, object]], *, limit: int = 359) -> list[dict[str, object]]:
    """Tally vendors for the tsv_format module.

    Entries without a ``vendor_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "vendor_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="tsv_format-10"))
        if len(selected) >= limit:
            break
    return selected

def validate_cartons(items: list[dict[str, object]], *, limit: int = 265) -> list[dict[str, object]]:
    """Validate cartons for the tsv_format module.

    Entries without a ``carton_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "carton_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="tsv_format-11"))
        if len(selected) >= limit:
            break
    return selected

def tally_cartons(items: list[dict[str, object]], *, limit: int = 264) -> list[dict[str, object]]:
    """Tally cartons for the tsv_format module.

    Entries without a ``carton_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "carton_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="tsv_format-12"))
        if len(selected) >= limit:
            break
    return selected

def split_lanes(items: list[dict[str, object]], *, limit: int = 272) -> list[dict[str, object]]:
    """Split lanes for the tsv_format module.

    Entries without a ``lane_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "lane_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="tsv_format-13"))
        if len(selected) >= limit:
            break
    return selected


def parse_rows(text: str) -> list[list[str]]:
    """Split a tsv export into rows, dropping its single header row."""

    lines = text.splitlines()[1:]  # skip the header row
    return [line.split("\t") for line in lines if line]
