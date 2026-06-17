"""Shared core for the Abilium ETL cloud service.

Pipeline per report:
    queries.build(...)  -> ShopifyQL string (tabular, location-filtered)
    shopify_client.run -> DataFrame with analytics-UI column headers
    transforms.*        -> Sheet-ready tabs + chart specs
    sheets_writer.write -> new Google Sheet in the Shared Drive -> URL
"""
