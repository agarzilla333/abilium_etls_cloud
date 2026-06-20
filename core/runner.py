"""Orchestrate one report end-to-end: query -> Shopify -> transform -> Sheet."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

import requests

from . import queries
from .config import get_client, resolve_locations
from .sheets_writer import SheetsWriter
from .shopify_client import run_shopifyql
from .transforms import build_report

REPORTS = ["sales", "inventory"]


@dataclass
class RunResult:
    client_key: str
    client_name: str
    report: str
    during: str
    locations: List[str]
    url: str
    title: str


def run_report(
    client_key: str,
    report: str,
    *,
    folder_id: str,
    during: Optional[str] = None,
    locations: Optional[List[str]] = None,
    dedupe: bool = False,
    writer: Optional[SheetsWriter] = None,
    session: Optional[requests.Session] = None,
) -> RunResult:
    if report not in queries.REPORTS:
        raise KeyError(f"unknown report {report!r}; known: {sorted(queries.REPORTS)}")

    client = get_client(client_key)
    locs = resolve_locations(client, locations)
    during = during or queries.REPORTS[report].default_during

    query = queries.build(report, locs, during)
    df = run_shopifyql(client, query, session=session)
    rep = build_report(report, df, dedupe=dedupe)

    date_str = datetime.now(ZoneInfo("America/Chicago")).strftime("%m-%d-%Y")
    suffix = "" if locs == client.locations else " — " + ", ".join(locs)
    title = f"{client.name} — {report} — {during} — {date_str}{suffix}"
    url = (writer or SheetsWriter()).write(title, rep, folder_id)

    return RunResult(client.key, client.name, report, during, locs, url, title)
