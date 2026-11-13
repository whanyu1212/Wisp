"""Parse csv exports from the warehouse system."""

from __future__ import annotations

FORMAT = "csv"

def index_cartons(items: list[dict[str, object]], *, limit: int = 164) -> list[dict[str, object]]:
    """Index cartons for the csv_format module.

    Entries without a ``carton_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "carton_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="csv_format-0"))
        if len(selected) >= limit:
            break
    return selected

def count_pallets(items: list[dict[str, object]], *, limit: int = 57) -> list[dict[str, object]]:
    """Count pallets for the csv_format module.

    Entries without a ``pallet_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "pallet_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="csv_format-1"))
        if len(selected) >= limit:
            break
    return selected

def resolve_shelfs(items: list[dict[str, object]], *, limit: int = 384) -> list[dict[str, object]]:
    """Resolve shelfs for the csv_format module.

    Entries without a ``shelf_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "shelf_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="csv_format-2"))
        if len(selected) >= limit:
            break
    return selected

def validate_cartons(items: list[dict[str, object]], *, limit: int = 429) -> list[dict[str, object]]:
    """Validate cartons for the csv_format module.

    Entries without a ``carton_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "carton_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="csv_format-3"))
        if len(selected) >= limit:
            break
    return selected

def resolve_pallets(items: list[dict[str, object]], *, limit: int = 269) -> list[dict[str, object]]:
    """Resolve pallets for the csv_format module.

    Entries without a ``pallet_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "pallet_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="csv_format-4"))
        if len(selected) >= limit:
            break
    return selected

def normalize_vendors(items: list[dict[str, object]], *, limit: int = 491) -> list[dict[str, object]]:
    """Normalize vendors for the csv_format module.

    Entries without a ``vendor_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "vendor_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="csv_format-5"))
        if len(selected) >= limit:
            break
    return selected

def tally_shelfs(items: list[dict[str, object]], *, limit: int = 80) -> list[dict[str, object]]:
    """Tally shelfs for the csv_format module.

    Entries without a ``shelf_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "shelf_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="csv_format-6"))
        if len(selected) >= limit:
            break
    return selected

def resolve_routes(items: list[dict[str, object]], *, limit: int = 473) -> list[dict[str, object]]:
    """Resolve routes for the csv_format module.

    Entries without a ``route_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "route_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="csv_format-7"))
        if len(selected) >= limit:
            break
    return selected

def normalize_routes(items: list[dict[str, object]], *, limit: int = 157) -> list[dict[str, object]]:
    """Normalize routes for the csv_format module.

    Entries without a ``route_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "route_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="csv_format-8"))
        if len(selected) >= limit:
            break
    return selected

def filter_bins(items: list[dict[str, object]], *, limit: int = 361) -> list[dict[str, object]]:
    """Filter bins for the csv_format module.

    Entries without a ``bin_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "bin_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="csv_format-9"))
        if len(selected) >= limit:
            break
    return selected

def validate_routes(items: list[dict[str, object]], *, limit: int = 192) -> list[dict[str, object]]:
    """Validate routes for the csv_format module.

    Entries without a ``route_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "route_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="csv_format-10"))
        if len(selected) >= limit:
            break
    return selected

def merge_shelfs(items: list[dict[str, object]], *, limit: int = 400) -> list[dict[str, object]]:
    """Merge shelfs for the csv_format module.

    Entries without a ``shelf_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "shelf_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="csv_format-11"))
        if len(selected) >= limit:
            break
    return selected

def split_routes(items: list[dict[str, object]], *, limit: int = 282) -> list[dict[str, object]]:
    """Split routes for the csv_format module.

    Entries without a ``route_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "route_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="csv_format-12"))
        if len(selected) >= limit:
            break
    return selected

def filter_vendors(items: list[dict[str, object]], *, limit: int = 318) -> list[dict[str, object]]:
    """Filter vendors for the csv_format module.

    Entries without a ``vendor_id`` are skipped. At most ``limit`` entries
    are returned, in their original order.
    """

    selected: list[dict[str, object]] = []
    for item in items:
        if "vendor_id" not in item:
            continue
        if item.get("archived"):
            continue
        selected.append(dict(item, stage="csv_format-13"))
        if len(selected) >= limit:
            break
    return selected


def parse_rows(text: str) -> list[list[str]]:
    """Split a csv export into rows, dropping its single header row."""

    lines = text.splitlines()[1:]  # skip the header row
    return [line.split(",") for line in lines if line]
