"""Pandas transforms ported from the original monthly scripts.

Input is a DataFrame whose columns are the analytics-UI CSV headers (produced by
``shopify_client``). Output is a ``Report``: ordered, Sheet-ready tabs plus pie
``ChartSpec``s. The aggregation logic is preserved verbatim from
``process_sales_by_location_monthly.py`` and ``process_inventory_monthly.py``;
only the input (was ``pd.read_csv``) and the output (was ``pd.ExcelWriter``)
change.
"""
from __future__ import annotations

import collections
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

# --- Column headers (must match shopify_client.FIELD_TO_HEADER values exactly) ---
NET_SALES = "Net sales"
PRODUCT_VENDOR = "Product vendor"
PRODUCT_TYPE = "Product type"
SALES_CHANNEL = "Sales channel"
TITLE_AT_SALE = "Product title at time of sale"
PRODUCT_TITLE = "Product title"

INV_UNITS = "Ending inventory units (at location)"
INV_RETAIL = "Ending inventory retail value (at location)"
VARIANT_TITLE = "Product variant title"
VARIANT_SKU = "Product variant SKU"


@dataclass(frozen=True)
class ChartSpec:
    """A pie chart on ``tab``: one slice per ``label_col`` row, sized by ``value_col``."""
    tab: str
    title: str
    label_col: str
    value_col: str
    max_slices: int = 12


@dataclass
class Report:
    kind: str  # "sales" | "inventory"
    tabs: "OrderedDict[str, pd.DataFrame]" = field(default_factory=OrderedDict)
    charts: List[ChartSpec] = field(default_factory=list)
    # tab name -> indent level (0 = top) per data row, for tabs laid out as a
    # nested section hierarchy. Tabs absent here are flat (no indenting/bolding).
    sections: Dict[str, List[int]] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Sales
# --------------------------------------------------------------------------- #
def _mode_first(s: pd.Series):
    """Return the first mode (most frequent) value or NaN."""
    m = s.mode(dropna=True)
    return m.iat[0] if len(m) else np.nan


def _build_vendor_regex(vendors) -> str:
    """Match any vendor as a standalone token-ish span (bounded by non-alphanumerics)
    to reduce false positives like 'ob' in 'job'. Longest-first so longer names win."""
    vendors = sorted(vendors, key=len, reverse=True)
    parts = [rf"(?<![A-Za-z0-9]){re.escape(v)}(?![A-Za-z0-9])" for v in vendors]
    return "(" + "|".join(parts) + ")"


def _sectioned_table(
    df: pd.DataFrame, columns: List[str], label_header: str
) -> Tuple[pd.DataFrame, List[int]]:
    """Render a nested group-by as one indented table (a "section" per top-level
    group). Groups by ``columns`` in order; each level is sorted by Total Sales
    desc. ``units`` = 1 per row (group size), ``total`` = sum of Net sales.

    Returns ``(frame, levels)`` where ``frame`` has columns
    ``[label_header, "Units", "Total Sales"]`` and ``levels[i]`` is the indent
    depth (0 = top) of data row ``i``. Because every parent's Units/Total equal
    the sum of its children, each section ties back to its top-level total.
    """
    rows: List[list] = []
    levels: List[int] = []

    def recurse(sub: pd.DataFrame, depth: int) -> None:
        col = columns[depth]
        grp = (
            sub.groupby(col, dropna=False)[NET_SALES]
            .agg(units="size", total="sum")
            .sort_values("total", ascending=False)
        )
        for key, agg in grp.iterrows():
            rows.append(["    " * depth + str(key), int(agg["units"]), float(agg["total"])])
            levels.append(depth)
            if depth + 1 < len(columns):
                recurse(sub[sub[col] == key], depth + 1)

    recurse(df, 0)
    frame = pd.DataFrame(rows, columns=[label_header, "Units", "Total Sales"])
    return frame, levels


def sales_by_location(df: pd.DataFrame) -> Report:
    df = df.copy()

    # The ShopifyQL API returns all cells as strings; coerce the money column so
    # groupby .sum() adds instead of concatenating. (CSV input was auto-typed.)
    df[NET_SALES] = pd.to_numeric(df[NET_SALES], errors="coerce")

    # Normalize key text fields once (vectorized).
    title_key = df[TITLE_AT_SALE].astype(str).str.strip().str.lower()
    product_title_key = df[PRODUCT_TITLE].astype(str).str.strip().str.lower()
    vendor_clean = (
        df[PRODUCT_VENDOR]
        .where(df[PRODUCT_VENDOR].apply(lambda x: isinstance(x, str)))
        .str.strip()
        .str.lower()
    )

    # 1) Learn title_at_time -> most common vendor among rows that DO have a vendor.
    title_to_vendor = (
        pd.DataFrame({"title_key": title_key, "vendor": vendor_clean})
        .dropna(subset=["vendor"])
        .groupby("title_key")["vendor"]
        .apply(_mode_first)
    )
    vendor_filled = vendor_clean.copy()
    missing = vendor_filled.isna()
    vendor_filled.loc[missing] = title_key.loc[missing].map(title_to_vendor)

    # 2) Fallback: regex over known vendors, against title then product title.
    vendor_vocab = vendor_clean.dropna().unique().tolist()
    if vendor_vocab:
        vendor_regex = _build_vendor_regex(vendor_vocab)
        still_missing = vendor_filled.isna()
        if still_missing.any():
            vendor_filled.loc[still_missing] = title_key.loc[still_missing].str.extract(
                vendor_regex, expand=False
            )
        still_missing = vendor_filled.isna()
        if still_missing.any():
            vendor_filled.loc[still_missing] = product_title_key.loc[still_missing].str.extract(
                vendor_regex, expand=False
            )

    # Anything still missing -> bucket so we never drop sales rows.
    df["vendor_final"] = vendor_filled.fillna("unknown")
    # Normalized string keys for the section group-bys (no NaN, so the recursive
    # equality filter in _sectioned_table is safe).
    df["product_type_key"] = df[PRODUCT_TYPE].fillna("unknown").astype(str)
    df["channel_key"] = df[SALES_CHANNEL].fillna("unknown").astype(str)

    # Each tab is a nested hierarchy: the top level is the tab's headline grouping
    # (and what its pie chart plots); deeper levels break each section down so the
    # children sum back to their parent.
    vendor_tab, vendor_levels = _sectioned_table(
        df, ["vendor_final", "product_type_key"], "Vendor"
    )
    product_tab, product_levels = _sectioned_table(
        df, ["product_type_key", "vendor_final"], "Product Type"
    )
    channel_tab, channel_levels = _sectioned_table(
        df, ["channel_key", "vendor_final", "product_type_key"], "Sales Channel"
    )

    report = Report(kind="sales")
    report.tabs["By Vendor"] = vendor_tab
    report.tabs["By Product"] = product_tab
    report.tabs["By Channel"] = channel_tab
    report.sections = {
        "By Vendor": vendor_levels,
        "By Product": product_levels,
        "By Channel": channel_levels,
    }
    report.charts = [
        ChartSpec("By Vendor", "Total Sales by Vendor", "Vendor", "Total Sales"),
        ChartSpec("By Product", "Total Sales by Product Type", "Product Type", "Total Sales"),
        ChartSpec("By Channel", "Total Sales by Sales Channel", "Sales Channel", "Total Sales"),
    ]
    return report


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #
def _dedupe_inventory(df: pd.DataFrame) -> pd.DataFrame:
    """Two-tier dedupe of duplicate Shopify listings (was commented-out in the
    source). Exact dedupe by SKU where present; heuristic fallback for NaN-SKU
    rows sharing (vendor, variant_title, per-unit retail). Can slightly
    over-collapse — treat results as a lower bound until SKUs are populated."""
    df = df.copy()
    df["_per_unit"] = (df[INV_RETAIL] / df[INV_UNITS]).round(2)
    with_sku = df[df[VARIANT_SKU].notna()].drop_duplicates(
        subset=[PRODUCT_VENDOR, VARIANT_SKU, VARIANT_TITLE], keep="first"
    )
    without_sku = df[df[VARIANT_SKU].isna()].drop_duplicates(
        subset=[PRODUCT_VENDOR, VARIANT_TITLE, "_per_unit"], keep="first"
    )
    return pd.concat([with_sku, without_sku], ignore_index=True).drop(columns=["_per_unit"])


def _inv_group(df: pd.DataFrame, keys, labels) -> pd.DataFrame:
    keys = list(keys)
    g = (
        df.groupby(keys, as_index=False)
        .agg(units=(INV_UNITS, "sum"), retail=(INV_RETAIL, "sum"))
        .sort_values("retail", ascending=False)
        .reset_index(drop=True)
    )
    g.columns = list(labels) + ["Units", "Total Retail Value"]
    return g


def inventory_by_location(df: pd.DataFrame, *, dedupe: bool = False) -> Report:
    """Port of InventoryData.get_inventory.

    ``dedupe`` defaults to False to match the source script's current (commented-
    out) behavior; retail values are then an upper bound. Enable once SKUs are
    reliably populated upstream.
    """
    df = df.copy()
    df.dropna(
        subset=[PRODUCT_VENDOR, PRODUCT_TYPE, VARIANT_TITLE, INV_RETAIL, INV_UNITS],
        inplace=True,
    )
    df[INV_UNITS] = pd.to_numeric(df[INV_UNITS], errors="coerce")
    df[INV_RETAIL] = pd.to_numeric(df[INV_RETAIL], errors="coerce")
    df.dropna(subset=[INV_UNITS, INV_RETAIL], inplace=True)
    df[PRODUCT_VENDOR] = (
        df[PRODUCT_VENDOR].astype(str).str.replace(r"[:*/\\]", " ", regex=True).str.lower()
    )
    df = df[(df[INV_RETAIL] != 0) & (df[INV_UNITS] > 0)].copy()

    if dedupe:
        df = _dedupe_inventory(df)

    inventory_df = _inv_group(df, [PRODUCT_VENDOR], ["Designers"])
    by_product_df = _inv_group(df, [PRODUCT_TYPE], ["Product Type"])
    granular_df = _inv_group(
        df, [PRODUCT_VENDOR, PRODUCT_TYPE, VARIANT_TITLE], ["Product Vendor", "Product Type", "Product Title"]
    )

    report = Report(kind="inventory")
    report.tabs["inventory"] = inventory_df
    report.tabs["by product"] = by_product_df

    # One granular tab per designer, alphabetical (after the inventory + by product tabs).
    for designer in sorted(inventory_df["Designers"], key=lambda s: str(s).lower()):
        sub = granular_df[granular_df["Product Vendor"] == designer][
            ["Product Type", "Product Title", "Units", "Total Retail Value"]
        ].reset_index(drop=True)
        report.tabs[str(designer)] = sub  # sheets_writer sanitizes/uniquifies tab names

    report.charts = [
        ChartSpec("inventory", "Retail Value by Designer", "Designers", "Total Retail Value"),
        ChartSpec("by product", "Retail Value by Product Type", "Product Type", "Total Retail Value"),
    ]
    return report


def build_report(kind: str, df: pd.DataFrame, *, dedupe: bool = False) -> Report:
    if kind == "sales":
        return sales_by_location(df)
    if kind == "inventory":
        return inventory_by_location(df, dedupe=dedupe)
    raise KeyError(f"unknown report kind {kind!r}")
