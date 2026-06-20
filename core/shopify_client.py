"""Run a ShopifyQL string via the GraphQL Admin API ``shopifyqlQuery`` query and
return a DataFrame whose columns match the analytics-UI CSV headers the
transforms expect.

NOTE — verify against the live API (phase 0/2): the exact GraphQL response shape
for ``shopifyqlQuery`` (union of table/visualization responses, ``parseErrors``,
``tableData.columns/rowData``) is encoded below from Shopify's docs but has not
been exercised against a live store. The first real call should confirm the
field names in ``FIELD_TO_HEADER`` and that ``inventory_by_location`` is an
API-exposed dataset; adjust here if not.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd
import requests

from .config import Client, get_token

# ShopifyQL field name (API column) -> analytics-UI CSV header (transform input).
FIELD_TO_HEADER = {
    # sales
    "gross_sales": "Gross sales",
    "net_sales": "Net sales",
    "sale_id": "Sale id",
    "order_name": "Order name",
    "product_title_at_time_of_sale": "Product title at time of sale",
    "product_title": "Product title",
    "product_type": "Product type",
    "product_vendor": "Product vendor",
    "sales_channel": "Sales channel",
    "order_or_return": "Order or return",
    "is_discounted_sale": "Is discounted sale",
    # inventory_by_location
    "ending_inventory_units_at_location": "Ending inventory units (at location)",
    "ending_inventory_value_at_location": "Ending inventory value (at location)",
    "ending_inventory_retail_value_at_location": "Ending inventory retail value (at location)",
    "product_variant_sku": "Product variant SKU",
    "product_variant_title": "Product variant title",
}

_GRAPHQL = """
query RunShopifyQL($query: String!) {
  shopifyqlQuery(query: $query) {
    __typename
    parseErrors
    tableData {
      columns { name dataType displayName }
      rows
    }
  }
}
"""


class ShopifyQLError(RuntimeError):
    pass


def run_shopifyql(
    client: Client,
    query: str,
    *,
    token: Optional[str] = None,
    session: Optional[requests.Session] = None,
    timeout: int = 60,
) -> pd.DataFrame:
    """Execute ``query`` against ``client``'s store; return a header-mapped DataFrame."""
    token = token or get_token(client)
    url = f"https://{client.domain}/admin/api/{client.api_version}/graphql.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    http = session or requests

    resp = http.post(
        url, json={"query": _GRAPHQL, "variables": {"query": query}}, headers=headers, timeout=timeout
    )
    resp.raise_for_status()
    payload = resp.json()

    if payload.get("errors"):
        raise ShopifyQLError(f"GraphQL transport errors: {payload['errors']}")

    node = (payload.get("data") or {}).get("shopifyqlQuery")
    if node is None:
        raise ShopifyQLError(f"empty shopifyqlQuery response: {payload}")

    parse_errors = node.get("parseErrors") or []
    if parse_errors:
        raise ShopifyQLError(f"ShopifyQL parse errors: {parse_errors}\nquery:\n{query}")

    table = node.get("tableData")
    if not table:
        raise ShopifyQLError(
            f"no tableData in response (typename={node.get('__typename')!r}); "
            "the query may have returned a visualization or an unsupported dataset"
        )

    columns = [col["name"] for col in table["columns"]]
    rows = table.get("rows") or table.get("rowData") or []  # tolerate either field name
    df = pd.DataFrame(rows, columns=columns)
    return df.rename(columns={k: v for k, v in FIELD_TO_HEADER.items() if k in df.columns})
