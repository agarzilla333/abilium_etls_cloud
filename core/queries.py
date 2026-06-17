"""ShopifyQL query templates.

These mirror the queries originally run by hand in Shopify's analytics UI, with
three deliberate changes for the Admin API's ``shopifyqlQuery`` (which returns
tabular ``tableData``, not a visualization):

  * UI-only clauses removed: ``VISUALIZE`` and ``WITH TOTALS, CURRENCY 'USD'``.
  * A ``WHERE inventory_location_name IN (...)`` clause is ALWAYS present, built
    from the client's location list (a list of one for single-location clients).
  * ``DURING`` differs per report: sales aggregates a period (``last_month``);
    inventory is a point-in-time snapshot (``today``).

``GROUP BY`` granularity is preserved exactly from the originals so the sales
transform's ``units = 1 per row`` assumption still holds.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

# DURING values offered in the UI dropdown (native ShopifyQL keywords).
DURING_CHOICES = ["last_month", "this_month", "last_year", "this_year", "today"]


@dataclass(frozen=True)
class ReportSpec:
    dataset: str
    show: List[str]
    group_by: List[str]
    default_during: str
    order_by: Optional[str] = None


REPORTS = {
    "sales": ReportSpec(
        dataset="sales",
        show=["gross_sales", "net_sales"],
        group_by=[
            "sale_id",
            "order_name",
            "product_title_at_time_of_sale",
            "product_type",
            "product_title",
            "sales_channel",
            "order_or_return",
            "is_discounted_sale",
            "product_vendor",
        ],
        default_during="last_month",
    ),
    "inventory": ReportSpec(
        dataset="inventory_by_location",
        show=[
            "ending_inventory_units_at_location",
            "ending_inventory_value_at_location",
            "ending_inventory_retail_value_at_location",
        ],
        group_by=[
            "product_variant_sku",
            "product_variant_title",
            "product_type",
            "product_vendor",
        ],
        default_during="today",
        order_by="ending_inventory_value_at_location DESC",
    ),
}


def _location_filter(locations: List[str]) -> str:
    if not locations:
        raise ValueError("at least one location is required")
    quoted = ", ".join("'" + loc.replace("'", "''") + "'" for loc in locations)
    return f"WHERE inventory_location_name IN ({quoted})"


def build(report: str, locations: List[str], during: Optional[str] = None) -> str:
    """Render a ShopifyQL string for ``report`` over ``locations``."""
    if report not in REPORTS:
        raise KeyError(f"unknown report {report!r}; known: {sorted(REPORTS)}")
    spec = REPORTS[report]
    during = during or spec.default_during

    parts = [
        f"FROM {spec.dataset}",
        "  SHOW " + ", ".join(spec.show),
        "  GROUP BY " + ", ".join(spec.group_by),
        "  " + _location_filter(locations),
        f"  DURING {during}",
    ]
    if spec.order_by:
        parts.append(f"  ORDER BY {spec.order_by}")
    return "\n".join(parts)
