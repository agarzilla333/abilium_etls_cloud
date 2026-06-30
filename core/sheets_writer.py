"""Write a Report to a fresh Google Sheet in the Shared Drive, with pie charts.

Auth: Application Default Credentials (the Cloud Run runtime service account).
No exported key — the runtime SA is added as a member of the Shared Drive, so it
can create files there. Locally, ``gcloud auth application-default login`` (with a
user who can write to the Drive) works for testing.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List

import google.auth
from googleapiclient.discovery import build as build_service

from .transforms import Report

log = logging.getLogger("sheets_writer")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
_SHEETS_MIME = "application/vnd.google-apps.spreadsheet"

# Columns formatted as currency ($#,##0.00) in the output sheet.
_MONEY_COLUMNS = {"Total Sales", "Total Retail Value"}

# First of two far-right helper columns (hidden) holding a contiguous copy of a
# sectioned tab's top-level rows, so its pie chart can source a contiguous range.
_HELPER_COL = 26  # column AA, clear of the report's narrow data columns

# Chars Google Sheets disallows in a tab title.
_BAD_TAB_CHARS = re.compile(r"[:\\/?*\[\]]")


def _sanitize_tab(name: str, used: set) -> str:
    """Make a tab title valid (no reserved chars, <=100, non-empty) and unique."""
    title = _BAD_TAB_CHARS.sub(" ", name).strip().strip("'") or "sheet"
    title = title[:100]
    base, n = title, 2
    while title.lower() in used:
        suffix = f" ({n})"
        title = base[: 100 - len(suffix)] + suffix
        n += 1
    used.add(title.lower())
    return title


def _col_letter(idx0: int) -> str:
    """0-based column index -> A1 letter(s)."""
    s, i = "", idx0
    while True:
        s = chr(ord("A") + i % 26) + s
        i = i // 26 - 1
        if i < 0:
            return s


class SheetsWriter:
    def __init__(self, credentials=None):
        if credentials is None:
            credentials, _ = google.auth.default(scopes=SCOPES)
        self.drive = build_service("drive", "v3", credentials=credentials, cache_discovery=False)
        self.sheets = build_service("sheets", "v4", credentials=credentials, cache_discovery=False)

    def write(self, title: str, report: Report, folder_id: str) -> str:
        """Create the spreadsheet, fill tabs, add charts; return its URL."""
        meta = self.drive.files().create(
            body={"name": title, "mimeType": _SHEETS_MIME, "parents": [folder_id]},
            fields="id, webViewLink",
            supportsAllDrives=True,
        ).execute()
        spreadsheet_id = meta["id"]

        default_sheet_id = self.sheets.spreadsheets().get(
            spreadsheetId=spreadsheet_id, fields="sheets.properties.sheetId"
        ).execute()["sheets"][0]["properties"]["sheetId"]

        # 1) Create all tabs (then drop the default), tracking title -> sheetId.
        used: set = set()
        tab_titles: Dict[str, str] = {}       # original tab key -> sanitized title
        sheet_ids: Dict[str, int] = {}        # sanitized title -> sheetId
        add_requests: List[dict] = []
        for i, key in enumerate(report.tabs):
            sid = 1000 + i
            sanitized = _sanitize_tab(key, used)
            tab_titles[key] = sanitized
            sheet_ids[sanitized] = sid
            props = {"sheetId": sid, "title": sanitized}
            if key in report.sections:
                # A new tab is only 26 columns (A–Z); widen sectioned tabs so the
                # far-right helper columns (_HELPER_COL, +1) for the chart exist.
                rows_needed = len(report.tabs[key]) + 2
                props["gridProperties"] = {"rowCount": max(1000, rows_needed), "columnCount": _HELPER_COL + 2}
            add_requests.append({"addSheet": {"properties": props}})
        add_requests.append({"deleteSheet": {"sheetId": default_sheet_id}})
        self.sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": add_requests}
        ).execute()

        # 2) Write values per tab (header row + data).
        value_data = []
        for key, df in report.tabs.items():
            values = [list(df.columns)] + df.astype(object).where(df.notna(), "").values.tolist()
            value_data.append({"range": f"'{tab_titles[key]}'!A1", "values": values})
        if value_data:
            self.sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"valueInputOption": "RAW", "data": value_data},
            ).execute()

        # 2b) For sectioned tabs the chart must plot only the top-level rows, but
        # those are interleaved with their children and the Sheets API rejects
        # discontiguous chart ranges. So stage each tab's top-level (label, value)
        # pairs as a *contiguous* block in two far-right helper columns (hidden in
        # step 3) and source the pie from there.
        helper_values = []
        chart_helpers: Dict[str, int] = {}  # tab name -> number of helper rows
        for chart in report.charts:
            levels = report.sections.get(chart.tab)
            df = report.tabs.get(chart.tab)
            if not levels or df is None or df.empty:
                continue
            label_idx = df.columns.get_loc(chart.label_col)
            value_idx = df.columns.get_loc(chart.value_col)
            parents = [i for i, lvl in enumerate(levels) if lvl == 0][: chart.max_slices]
            block = [[str(df.iat[r, label_idx]), float(df.iat[r, value_idx])] for r in parents]
            if not block:
                continue
            helper_values.append({
                "range": f"'{tab_titles[chart.tab]}'!{_col_letter(_HELPER_COL)}1",
                "values": block,
            })
            chart_helpers[chart.tab] = len(block)
        if helper_values:
            self.sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"valueInputOption": "RAW", "data": helper_values},
            ).execute()

        # 3) Format money columns as currency + bold header, then add pie charts.
        requests = []
        for key, df in report.tabs.items():
            sid = sheet_ids[tab_titles[key]]
            # bold the header row
            requests.append({
                "repeatCell": {
                    "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat.textFormat.bold",
                }
            })
            # On sectioned tabs, bold every section-parent row (any row with a
            # deeper-indented row beneath it). Data rows start at sheet row 1.
            levels = report.sections.get(key)
            if levels:
                for i, lvl in enumerate(levels):
                    is_parent = i + 1 < len(levels) and levels[i + 1] > lvl
                    if is_parent:
                        requests.append({
                            "repeatCell": {
                                "range": {"sheetId": sid, "startRowIndex": i + 1, "endRowIndex": i + 2},
                                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                                "fields": "userEnteredFormat.textFormat.bold",
                            }
                        })
            for ci, colname in enumerate(df.columns):
                if colname in _MONEY_COLUMNS:
                    requests.append({
                        "repeatCell": {
                            "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": len(df) + 1,
                                      "startColumnIndex": ci, "endColumnIndex": ci + 1},
                            "cell": {"userEnteredFormat": {"numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00"}}},
                            "fields": "userEnteredFormat.numberFormat",
                        }
                    })

        for chart in report.charts:
            df = report.tabs.get(chart.tab)
            if df is None or df.empty:
                continue
            sid = sheet_ids[tab_titles[chart.tab]]
            n_cols = len(df.columns)
            if chart.tab in chart_helpers:
                # Source from the contiguous hidden helper block (cols _HELPER_COL,
                # _HELPER_COL+1), then hide those columns.
                n_rows = chart_helpers[chart.tab]
                requests.append(self._pie_request(chart.title, sid, _HELPER_COL, _HELPER_COL + 1, n_rows, n_cols, header=False))
                requests.append({
                    "updateDimensionProperties": {
                        "range": {"sheetId": sid, "dimension": "COLUMNS",
                                  "startIndex": _HELPER_COL, "endIndex": _HELPER_COL + 2},
                        "properties": {"hiddenByUser": True},
                        "fields": "hiddenByUser",
                    }
                })
            else:
                label_idx = df.columns.get_loc(chart.label_col)
                value_idx = df.columns.get_loc(chart.value_col)
                n_rows = min(len(df), chart.max_slices)
                requests.append(
                    self._pie_request(chart.title, sid, label_idx, value_idx, n_rows, n_cols)
                )

        if requests:
            self.sheets.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": requests}
            ).execute()

        # 4) Collapsible outline for sectioned tabs: group each parent's block of
        # descendants and collapse it, so the tab opens at its top level and each
        # level expands on demand. Data row d sits at sheet row d+1 (header at 0),
        # so a parent at data index p groups sheet rows [p+2, e+1) — its deeper-
        # indented rows, the parent itself left visible. `depth` is 1-indexed by
        # nesting (the API requires depth > 0): the outermost group is 1, so a
        # parent at indent level L groups at depth L+1. Run as its own best-effort
        # batch so a grouping hiccup never costs the already-applied data, currency,
        # and charts.
        group_adds, group_collapses = [], []
        for key, levels in report.sections.items():
            sid = sheet_ids[tab_titles[key]]
            n = len(levels)
            for p in range(n):
                lvl = levels[p]
                e = p + 1
                while e < n and levels[e] > lvl:
                    e += 1
                if e == p + 1:
                    continue  # leaf row, nothing to group
                rng = {"sheetId": sid, "dimension": "ROWS", "startIndex": p + 2, "endIndex": e + 1}
                group_adds.append({"addDimensionGroup": {"range": rng}})
                group_collapses.append({
                    "updateDimensionGroup": {
                        "dimensionGroup": {"range": rng, "depth": lvl + 1, "collapsed": True},
                        "fields": "collapsed",
                    }
                })
        if group_adds:
            try:  # add every group first, then collapse them
                self.sheets.spreadsheets().batchUpdate(
                    spreadsheetId=spreadsheet_id, body={"requests": group_adds + group_collapses}
                ).execute()
            except Exception:  # noqa: BLE001 — grouping is cosmetic; keep the report
                log.exception("row grouping failed for %s; report written without it", title)

        return meta.get("webViewLink") or f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"

    def delete_reports(self, folder_id: str) -> int:
        """Trash every report spreadsheet in the Shared Drive. Returns count.

        Uses trash (not permanent delete) because the runtime SA is a Content
        Manager, which can trash but not permanently delete in a Shared Drive.
        Trashed sheets leave the Drive view and the Shared Drive trash auto-purges
        after 30 days (or a Manager can empty it for immediate removal)."""
        trashed, page = 0, None
        while True:
            resp = self.drive.files().list(
                q=f"'{folder_id}' in parents and mimeType='{_SHEETS_MIME}' and trashed=false",
                corpora="drive", driveId=folder_id,
                includeItemsFromAllDrives=True, supportsAllDrives=True,
                fields="nextPageToken, files(id)", pageSize=1000, pageToken=page,
            ).execute()
            for f in resp.get("files", []):
                self.drive.files().update(
                    fileId=f["id"], body={"trashed": True}, supportsAllDrives=True
                ).execute()
                trashed += 1
            page = resp.get("nextPageToken")
            if not page:
                break
        return trashed

    @staticmethod
    def _pie_request(title, sheet_id, label_idx, value_idx, n_rows, n_cols, header=True) -> dict:
        """A pie chart over a contiguous block. ``header=True`` includes row 0 as
        the series/domain label (flat tabs); ``header=False`` treats row 0 as the
        first data row (the sectioned tabs' headerless helper block)."""
        start = 0
        end = n_rows + 1 if header else n_rows

        def col_range(col_idx):
            return {
                "sources": [{
                    "sheetId": sheet_id,
                    "startRowIndex": start,
                    "endRowIndex": end,
                    "startColumnIndex": col_idx,
                    "endColumnIndex": col_idx + 1,
                }]
            }

        return {
            "addChart": {
                "chart": {
                    "spec": {
                        "title": title,
                        # Plot the source cells even when hidden — the helper columns
                        # are hidden and the section rows collapse into outline groups.
                        "hiddenDimensionStrategy": "SHOW_ALL",
                        "pieChart": {
                            # LABELED_LEGEND labels each slice with its name + percentage on a leader line
                            "legendPosition": "LABELED_LEGEND",
                            "domain": {"sourceRange": col_range(label_idx)},
                            "series": {"sourceRange": col_range(value_idx)},
                        },
                    },
                    "position": {
                        "overlayPosition": {
                            "anchorCell": {
                                "sheetId": sheet_id,
                                "rowIndex": 1,
                                "columnIndex": n_cols + 1,
                            }
                        }
                    },
                }
            }
        }
