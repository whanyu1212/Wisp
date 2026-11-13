"""Parse json exports from the warehouse system."""

from __future__ import annotations

FORMAT = "json"

def split_vendors(items: list[dict[str, object]], *, limit: int = 423) -> list[dict[str, object]]:
    """Split vendors for the json_format module.

    Entries without a ``vendor_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "vendor_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="json_format-0"))
        if len(selected) >= limit:
            break
    return selected

def group_lanes(items: list[dict[str, object]], *, limit: int = 416) -> list[dict[str, object]]:
    """Group lanes for the json_format module.

    Entries without a ``lane_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "lane_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="json_format-1"))
        if len(selected) >= limit:
            break
    return selected

def tally_cartons(items: list[dict[str, object]], *, limit: int = 187) -> list[dict[str, object]]:
    """Tally cartons for the json_format module.

    Entries without a ``carton_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "carton_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="json_format-2"))
        if len(selected) >= limit:
            break
    return selected

def resolve_skus(items: list[dict[str, object]], *, limit: int = 19) -> list[dict[str, object]]:
    """Resolve skus for the json_format module.

    Entries without a ``sku_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "sku_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="json_format-3"))
        if len(selected) >= limit:
            break
    return selected

def validate_cartons(items: list[dict[str, object]], *, limit: int = 137) -> list[dict[str, object]]:
    """Validate cartons for the json_format module.

    Entries without a ``carton_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "carton_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="json_format-4"))
        if len(selected) >= limit:
            break
    return selected

def split_lanes(items: list[dict[str, object]], *, limit: int = 314) -> list[dict[str, object]]:
    """Split lanes for the json_format module.

    Entries without a ``lane_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "lane_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="json_format-5"))
        if len(selected) >= limit:
            break
    return selected

def rank_cartons(items: list[dict[str, object]], *, limit: int = 418) -> list[dict[str, object]]:
    """Rank cartons for the json_format module.

    Entries without a ``carton_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "carton_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="json_format-6"))
        if len(selected) >= limit:
            break
    return selected

def resolve_shelfs(items: list[dict[str, object]], *, limit: int = 493) -> list[dict[str, object]]:
    """Resolve shelfs for the json_format module.

    Entries without a ``shelf_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "shelf_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="json_format-7"))
        if len(selected) >= limit:
            break
    return selected

def rank_bins(items: list[dict[str, object]], *, limit: int = 117) -> list[dict[str, object]]:
    """Rank bins for the json_format module.

    Entries without a ``bin_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "bin_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="json_format-8"))
        if len(selected) >= limit:
            break
    return selected

def count_vendors(items: list[dict[str, object]], *, limit: int = 245) -> list[dict[str, object]]:
    """Count vendors for the json_format module.

    Entries without a ``vendor_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "vendor_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="json_format-9"))
        if len(selected) >= limit:
            break
    return selected

def split_shelfs(items: list[dict[str, object]], *, limit: int = 109) -> list[dict[str, object]]:
    """Split shelfs for the json_format module.

    Entries without a ``shelf_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "shelf_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="json_format-10"))
        if len(selected) >= limit:
            break
    return selected

def index_depots(items: list[dict[str, object]], *, limit: int = 465) -> list[dict[str, object]]:
    """Index depots for the json_format module.

    Entries without a ``depot_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "depot_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="json_format-11"))
        if len(selected) >= limit:
            break
    return selected

def scan_skus(items: list[dict[str, object]], *, limit: int = 250) -> list[dict[str, object]]:
    """Scan skus for the json_format module.

    Entries without a ``sku_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "sku_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="json_format-12"))
        if len(selected) >= limit:
            break
    return selected

def filter_shelfs(items: list[dict[str, object]], *, limit: int = 414) -> list[dict[str, object]]:
    """Filter shelfs for the json_format module.

    Entries without a ``shelf_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "shelf_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="json_format-13"))
        if len(selected) >= limit:
            break
    return selected


def parse_rows(text: str) -> list[list[str]]:
    """Split a json export into rows, dropping its single header row."""

    lines = text.splitlines()[1:]  # skip the header row
    return [line.split("=") for line in lines if line]
