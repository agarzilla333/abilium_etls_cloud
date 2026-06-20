"""Synthetic smoke test for the transforms — no GCP/Shopify needed.

Run: python -m tests.smoke
Validates the ported sales + inventory logic against a tiny hand-checkable frame.
"""
import pandas as pd

from core import queries
from core.transforms import inventory_by_location, sales_by_location


def test_queries():
    q = queries.build("sales", ["Kick Pleat Austin", "Kick Pleat Dallas"])
    assert "FROM sales" in q
    assert "WHERE" not in q  # sales is store-wide (includes online)
    assert "DURING last_month" in q
    assert "LIMIT 1000000" in q
    assert "VISUALIZE" not in q and "WITH TOTALS" not in q

    inv = queries.build("inventory", ["Austin Store"])
    assert "FROM inventory_by_location" in inv
    assert "WHERE inventory_location_name IN ('Austin Store')" in inv
    assert "DURING today" in inv
    print("queries: OK")


def test_sales():
    df = pd.DataFrame({
        "Product title at time of sale": ["Acme Tee", "Acme Tee", "Globex Mug", "Mystery Item"],
        "Product title": ["Acme Tee", "Acme Tee", "Globex Mug", "Acme special"],
        "Product vendor": ["Acme", None, "Globex", None],  # row 2 backfilled via title mode; row 4 via regex
        "Product type": ["Apparel", "Apparel", "Drinkware", "Apparel"],
        "Net sales": [10.0, 20.0, 5.0, 7.0],
        "Sales channel": ["Online", "Online", "POS", "POS"],
    })
    rep = sales_by_location(df)
    by_vendor = rep.tabs["By Vendor"].set_index("Vendor")
    # Acme: rows 1,2 (backfill) + row 4 (regex from "acme special") = 3 units, 37.0
    assert int(by_vendor.loc["acme", "Units"]) == 3, by_vendor
    assert abs(by_vendor.loc["acme", "Total Sales"] - 37.0) < 1e-9, by_vendor
    assert int(by_vendor.loc["globex", "Units"]) == 1
    assert set(rep.tabs) == {"By Vendor", "By Product", "By Channel"}
    print("sales: OK")


def test_inventory():
    df = pd.DataFrame({
        "Product vendor": ["Jungmaven", "Jungmaven", "Hanky Panky", "Drop Me"],
        "Product type": ["Hemp", "Hemp", "Lingerie", "X"],
        "Product variant title": ["Tee S", "Tee M", "Brief", "Y"],
        "Product variant SKU": ["JM1", "JM2", "HP1", "D1"],
        "Ending inventory units (at location)": [3, 2, 5, 0],          # last row dropped (units 0)
        "Ending inventory retail value (at location)": [30.0, 20.0, 50.0, 99.0],
    })
    rep = inventory_by_location(df)
    inv = rep.tabs["inventory"].set_index("Designers")
    assert abs(inv.loc["jungmaven", "Total Retail Value"] - 50.0) < 1e-9, inv
    assert int(inv.loc["jungmaven", "Units"]) == 5
    assert "drop me" not in inv.index  # units==0 filtered out
    # one granular tab per designer present
    assert "jungmaven" in rep.tabs and "hanky panky" in rep.tabs
    print("inventory: OK")


if __name__ == "__main__":
    test_queries()
    test_sales()
    test_inventory()
    print("\nAll smoke checks passed.")
