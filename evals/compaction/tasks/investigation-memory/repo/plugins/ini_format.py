"""Parse ini exports from the warehouse system."""

from __future__ import annotations

FORMAT = "ini"

def validate_routes(items: list[dict[str, object]], *, limit: int = 462) -> list[dict[str, object]]:
    """Validate routes for the ini_format module.

    Entries without a ``route_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "route_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="ini_format-0"))
        if len(selected) >= limit:
            break
    return selected

def split_cartons(items: list[dict[str, object]], *, limit: int = 75) -> list[dict[str, object]]:
    """Split cartons for the ini_format module.

    Entries without a ``carton_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "carton_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="ini_format-1"))
        if len(selected) >= limit:
            break
    return selected

def group_bins(items: list[dict[str, object]], *, limit: int = 205) -> list[dict[str, object]]:
    """Group bins for the ini_format module.

    Entries without a ``bin_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "bin_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="ini_format-2"))
        if len(selected) >= limit:
            break
    return selected

def index_shelfs(items: list[dict[str, object]], *, limit: int = 42) -> list[dict[str, object]]:
    """Index shelfs for the ini_format module.

    Entries without a ``shelf_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "shelf_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="ini_format-3"))
        if len(selected) >= limit:
            break
    return selected

def filter_vendors(items: list[dict[str, object]], *, limit: int = 224) -> list[dict[str, object]]:
    """Filter vendors for the ini_format module.

    Entries without a ``vendor_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "vendor_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="ini_format-4"))
        if len(selected) >= limit:
            break
    return selected

def count_vendors(items: list[dict[str, object]], *, limit: int = 347) -> list[dict[str, object]]:
    """Count vendors for the ini_format module.

    Entries without a ``vendor_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "vendor_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="ini_format-5"))
        if len(selected) >= limit:
            break
    return selected

def validate_bins(items: list[dict[str, object]], *, limit: int = 464) -> list[dict[str, object]]:
    """Validate bins for the ini_format module.

    Entries without a ``bin_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "bin_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="ini_format-6"))
        if len(selected) >= limit:
            break
    return selected

def merge_lanes(items: list[dict[str, object]], *, limit: int = 334) -> list[dict[str, object]]:
    """Merge lanes for the ini_format module.

    Entries without a ``lane_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "lane_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="ini_format-7"))
        if len(selected) >= limit:
            break
    return selected

def filter_shelfs(items: list[dict[str, object]], *, limit: int = 78) -> list[dict[str, object]]:
    """Filter shelfs for the ini_format module.

    Entries without a ``shelf_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "shelf_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="ini_format-8"))
        if len(selected) >= limit:
            break
    return selected

def validate_pallets(items: list[dict[str, object]], *, limit: int = 500) -> list[dict[str, object]]:
    """Validate pallets for the ini_format module.

    Entries without a ``pallet_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "pallet_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="ini_format-9"))
        if len(selected) >= limit:
            break
    return selected

def index_vendors(items: list[dict[str, object]], *, limit: int = 387) -> list[dict[str, object]]:
    """Index vendors for the ini_format module.

    Entries without a ``vendor_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "vendor_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="ini_format-10"))
        if len(selected) >= limit:
            break
    return selected

def count_lots(items: list[dict[str, object]], *, limit: int = 458) -> list[dict[str, object]]:
    """Count lots for the ini_format module.

    Entries without a ``lot_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "lot_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="ini_format-11"))
        if len(selected) >= limit:
            break
    return selected

def index_pallets(items: list[dict[str, object]], *, limit: int = 346) -> list[dict[str, object]]:
    """Index pallets for the ini_format module.

    Entries without a ``pallet_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "pallet_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="ini_format-12"))
        if len(selected) >= limit:
            break
    return selected

def split_pallets(items: list[dict[str, object]], *, limit: int = 366) -> list[dict[str, object]]:
    """Split pallets for the ini_format module.

    Entries without a ``pallet_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "pallet_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="ini_format-13"))
        if len(selected) >= limit:
            break
    return selected


def parse_rows(text: str) -> list[list[str]]:
    """Split a ini export into rows, dropping its single header row."""

    lines = text.splitlines()[1:]  # skip the header row
    return [line.split("=") for line in lines if line]
