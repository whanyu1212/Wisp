"""Synthetic MCP tool catalogs for the MCP tasks.

Each catalog is the tool list of one stdio server that `serve.py` starts. The
runner reads the same data to allow the tools and to tell when a server failed
to start, so nothing here may import Wisp or the MCP SDK.

Decoy tools answer with plausible, wrong values instead of errors: an error
would let a model try tools until one works, which does not test whether it
picked the right one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping

from evals.tool_use.servers.devhub import DEVHUB
from evals.tool_use.servers.model import Arguments, Catalog, CatalogTool, Parameter

__all__ = ["CATALOGS", "Arguments", "Catalog", "CatalogTool", "Parameter"]


def _number(*parts: str, low: int, high: int) -> int:
    """A stable number in [low, high] for the given arguments."""

    digest = hashlib.sha256("/".join(parts).encode()).digest()
    return low + int.from_bytes(digest[:4], "big") % (high - low + 1)


# The inventory task asks for SKU A-1172's reorder point in Lisbon.
REORDER_SKU = "A-1172"
REORDER_WAREHOUSE = "lisbon"
REORDER_POINT = 140


def _place(arguments: Arguments) -> tuple[str, str]:
    """The SKU and warehouse, spelled the way the catalog stores them."""

    return str(arguments["sku"]).strip().upper(), str(arguments["warehouse"]).strip().lower()


def _reorder_policy(arguments: Arguments) -> str:
    sku, warehouse = _place(arguments)
    if (sku, warehouse) == (REORDER_SKU, REORDER_WAREHOUSE):
        point = REORDER_POINT
    else:
        point = _number("reorder", sku, warehouse, low=50, high=400)
    return f"{sku} at {warehouse}: reorder point {point} units; order quantity {point * 4} units"


def _measure(label: str, unit: str, *, low: int, high: int) -> Callable[[Arguments], str]:
    def respond(arguments: Arguments) -> str:
        sku, warehouse = _place(arguments)
        value = _number(label, sku, warehouse, low=low, high=high)
        return f"{sku} at {warehouse}: {label} {value} {unit}"

    return respond


# (name, description, label, unit, low, high). Several are close to the reorder
# point in meaning or value, so a model has to read descriptions, not names.
_INVENTORY_DECOYS = (
    (
        "get_stock_level",
        "Units on hand for a SKU at a warehouse right now.",
        "on hand",
        "units",
        80,
        900,
    ),
    (
        "get_stock_history",
        "Average units on hand over the last 30 days.",
        "30-day average",
        "units",
        80,
        900,
    ),
    (
        "get_stock_forecast",
        "Forecast units on hand in 14 days.",
        "14-day forecast",
        "units",
        60,
        900,
    ),
    (
        "get_safety_stock",
        "Buffer stock kept against demand spikes; not an order trigger.",
        "safety stock",
        "units",
        60,
        160,
    ),
    (
        "get_min_max_levels",
        "Minimum display level shown on the stock dashboard.",
        "dashboard minimum",
        "units",
        100,
        180,
    ),
    (
        "get_reorder_history",
        "Size of the most recent purchase order.",
        "last order",
        "units",
        200,
        900,
    ),
    (
        "get_reorder_suggestions",
        "Units the planner suggests ordering today.",
        "suggested order",
        "units",
        100,
        700,
    ),
    (
        "get_reorder_lead_time",
        "Days between placing an order and receiving it.",
        "lead time",
        "days",
        2,
        30,
    ),
    (
        "get_backorder_count",
        "Units owed to customers and not yet shipped.",
        "backordered",
        "units",
        0,
        200,
    ),
    ("get_reserved_stock", "Units reserved for open orders.", "reserved", "units", 0, 300),
    ("get_inbound_shipments", "Units on the way from suppliers.", "inbound", "units", 0, 800),
    ("get_outbound_shipments", "Units leaving the warehouse today.", "outbound", "units", 0, 400),
    ("get_damaged_stock", "Units marked damaged and not sellable.", "damaged", "units", 0, 40),
    (
        "get_returns_pending",
        "Customer returns waiting for inspection.",
        "returns pending",
        "units",
        0,
        60,
    ),
    ("get_daily_demand", "Average units sold per day.", "daily demand", "units", 5, 80),
    ("get_weekly_demand", "Average units sold per week.", "weekly demand", "units", 30, 500),
    (
        "get_demand_variance",
        "Standard deviation of daily demand.",
        "demand deviation",
        "units",
        1,
        30,
    ),
    ("get_service_level", "Target in-stock rate, in percent.", "service level", "percent", 90, 99),
    ("get_stockout_days", "Days out of stock in the last quarter.", "stockout days", "days", 0, 20),
    ("get_turnover_rate", "Inventory turns per year.", "turnover", "turns", 2, 24),
    (
        "get_holding_cost",
        "Yearly cost of holding one unit, in cents.",
        "holding cost",
        "cents",
        50,
        900,
    ),
    ("get_unit_cost", "Purchase cost of one unit, in cents.", "unit cost", "cents", 100, 9000),
    ("get_unit_price", "Selling price of one unit, in cents.", "unit price", "cents", 200, 15000),
    (
        "get_supplier_moq",
        "The supplier's minimum order quantity.",
        "minimum order",
        "units",
        50,
        500,
    ),
    ("get_pallet_size", "Units per pallet.", "pallet size", "units", 20, 240),
    ("get_case_size", "Units per case.", "case size", "units", 4, 48),
    ("get_bin_capacity", "Units that fit in the SKU's bin.", "bin capacity", "units", 100, 600),
    (
        "get_warehouse_capacity",
        "Free pallet positions in the warehouse.",
        "free positions",
        "pallets",
        10,
        900,
    ),
    ("get_pick_rate", "Units picked per hour for the SKU.", "pick rate", "units/hour", 20, 300),
    ("get_cycle_count", "Units found at the last cycle count.", "last count", "units", 80, 900),
    ("get_count_variance", "Difference at the last cycle count.", "count variance", "units", 0, 25),
    ("get_shrinkage", "Units lost in the last year.", "shrinkage", "units", 0, 50),
    ("get_expiry_soon", "Units expiring within 30 days.", "expiring soon", "units", 0, 100),
    ("get_quarantine_stock", "Units held for quality checks.", "quarantined", "units", 0, 60),
    ("get_transfer_in", "Units arriving from other warehouses.", "transfer in", "units", 0, 300),
    ("get_transfer_out", "Units leaving for other warehouses.", "transfer out", "units", 0, 300),
    (
        "get_promo_uplift",
        "Expected extra units sold during promotions.",
        "promo uplift",
        "units",
        0,
        400,
    ),
    (
        "get_seasonal_index",
        "Seasonal demand index, in percent of average.",
        "seasonal index",
        "percent",
        70,
        140,
    ),
    ("get_abc_class", "ABC class rank of the SKU, 1 being most valuable.", "class rank", "", 1, 3),
    ("get_slotting_zone", "Pick zone number assigned to the SKU.", "zone", "", 1, 12),
    ("get_last_receipt", "Units in the last delivery received.", "last receipt", "units", 50, 900),
    ("get_open_purchase_orders", "Units on open purchase orders.", "on order", "units", 0, 1200),
    ("get_max_stock", "Upper stock limit for the SKU.", "maximum", "units", 400, 1500),
    (
        "get_economic_order_qty",
        "Economic order quantity.",
        "economic order quantity",
        "units",
        100,
        900,
    ),
    (
        "get_target_days_cover",
        "Days of demand the warehouse aims to hold.",
        "target cover",
        "days",
        7,
        60,
    ),
    (
        "get_projected_stockout",
        "Days until stock runs out at current demand.",
        "days to stockout",
        "days",
        1,
        90,
    ),
    (
        "get_substitute_stock",
        "Units of approved substitute SKUs on hand.",
        "substitute stock",
        "units",
        0,
        500,
    ),
    ("get_reorder_alerts", "Number of reorder alerts raised this month.", "alerts", "", 0, 9),
)


def _inventory(decoys: int = len(_INVENTORY_DECOYS)) -> Catalog:
    """The inventory catalog with its first ``decoys`` decoy tools.

    Every size keeps the reorder tool and `list_warehouses`, so the inventory task
    has the same answer at any size; the smaller ones measure where on-demand
    discovery starts to pay off.
    """

    return Catalog(
        parameters=("sku", "warehouse"),
        tools=(
            CatalogTool(
                "get_reorder_policy",
                "The reorder point (the stock level at which a new purchase order is placed) "
                "and the order quantity for a SKU at a warehouse.",
                _reorder_policy,
            ),
            *(
                CatalogTool(name, description, _measure(label, unit, low=low, high=high))
                for name, description, label, unit, low, high in _INVENTORY_DECOYS[:decoys]
            ),
            CatalogTool(
                "list_warehouses",
                "Warehouse identifiers, for the warehouse parameter.",
                lambda _arguments: "lisbon, porto, madrid, lyon",
            ),
        ),
    )


INVENTORY = _inventory()
# Smaller copies of the inventory catalog for the catalog-size sweep, by tool count.
INVENTORY_SWEEP_SIZES = (6, 12, 20, 30)

# The billing task asks for invoice INV-2207's current total from the live system.
INVOICE_ID = "INV-2207"
LIVE_TOTAL = "1880.40"


def _billing(system: str, total: str, note: str) -> Catalog:
    def invoice(arguments: Arguments) -> str:
        if str(arguments["invoice_id"]).strip().upper() != INVOICE_ID:
            return f"{system}: no invoice {arguments['invoice_id']}"
        return f"{system}: invoice {INVOICE_ID} for Northwind Traders, total {total} EUR"

    def customer(arguments: Arguments) -> str:
        return f"{system}: invoice {arguments['invoice_id']} belongs to Northwind Traders"

    return Catalog(
        parameters=("invoice_id",),
        tools=(
            CatalogTool("get_invoice", f"Look up an invoice's total. {note}", invoice),
            CatalogTool(
                "get_invoice_customer", f"The customer an invoice belongs to. {note}", customer
            ),
        ),
    )


CATALOGS: Mapping[str, Catalog] = {
    "inventory": INVENTORY,
    **{f"inventory-{size}": _inventory(size - 2) for size in INVENTORY_SWEEP_SIZES},
    "devhub": DEVHUB,
    "billing": _billing("billing", LIVE_TOTAL, "Live production billing system."),
    "billing-legacy": _billing(
        "billing-legacy",
        "1710.00",
        "Read-only archive of the billing system before the 2025 migration; totals are not "
        "updated after it.",
    ),
    "billing-sandbox": _billing(
        "billing-sandbox", "99.99", "Sandbox with made-up test data, for integration tests."
    ),
}
