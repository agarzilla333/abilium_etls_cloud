"""Write a Report to a fresh Google Sheet in the Shared Drive, with pie charts.

Auth: Application Default Credentials (the Cloud Run runtime service account).
No exported key — the runtime SA is added as a member of the Shared Drive, so it
can create files there. Locally, ``gcloud auth application-default login`` (with a
user who can write to the Drive) works for testing.
"""
from __future__ import annotations

import re
from typing import Dict, List

import google.auth
from googleapiclient.discovery import build as build_service

from .transforms import Report

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
_SHEETS_MIME = "application/vnd.google-apps.spreadsheet"

# Columns formatted as currency ($#,##0.00) in the output sheet.
_MONEY_COLUMNS = {"Total Sales", "Total Retail Value"}

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
            add_requests.append({"addSheet": {"properties": {"sheetId": sid, "title": sanitized}}})
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
            label_idx = df.columns.get_loc(chart.label_col)
            value_idx = df.columns.get_loc(chart.value_col)
            n_rows = min(len(df), chart.max_slices)
            requests.append(
                self._pie_request(chart.title, sid, label_idx, value_idx, n_rows, len(df.columns))
            )

        if requests:
            self.sheets.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": requests}
            ).execute()

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
    def _pie_request(title, sheet_id, label_idx, value_idx, n_rows, n_cols) -> dict:
        def col_range(col_idx):
            return {
                "sources": [{
                    "sheetId": sheet_id,
                    "startRowIndex": 0,          # include header row as the series/domain label
                    "endRowIndex": n_rows + 1,
                    "startColumnIndex": col_idx,
                    "endColumnIndex": col_idx + 1,
                }]
            }

        return {
            "addChart": {
                "chart": {
                    "spec": {
                        "title": title,
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
