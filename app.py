"""FastAPI service for the Abilium ETL cloud.

Endpoints:
  GET  /healthz   — liveness.
  POST /run       — UI-triggered single report. IAM-public; gated IN-APP by a
                    Google ID token (domain + allow-list). Needs CORS.
  POST /run-all   — Cloud Scheduler monthly batch. IAM-restricted to the
                    scheduler's OIDC service account (no in-app token check).

Env:
  DRIVE_FOLDER_ID      Shared Drive folder id for output sheets (required).
  OAUTH_CLIENT_ID      Google OAuth client id used by the Squarespace sign-in
                       button; the ID token's audience must match this.
  ALLOWED_DOMAIN       e.g. "abilium.com" — token email must end with @domain.
  ALLOWED_EMAILS       comma-separated final allow-list (optional but recommended).
  CORS_ORIGINS         comma-separated allowed origins (the Squarespace site).
  INVENTORY_DEDUPE     "1" to enable inventory dedupe (default off; see transforms).
"""
from __future__ import annotations

import logging
import os
import secrets
from typing import Dict, List, Optional

import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from google.auth.transport import requests as ga_requests
from google.oauth2 import id_token as google_id_token
from pydantic import BaseModel

from core import notify, oauth, queries
from core.config import (
    get_client,
    get_client_by_domain,
    load_clients,
    store_token,
)
from core.runner import REPORTS, RunResult, run_report
from core.sheets_writer import SheetsWriter

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

app = FastAPI(title="Abilium ETL")

_CORS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS or ["*"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)


def _folder_id() -> str:
    folder = os.environ.get("DRIVE_FOLDER_ID")
    if not folder:
        raise HTTPException(500, "DRIVE_FOLDER_ID not configured")
    return folder


def _dedupe() -> bool:
    return os.environ.get("INVENTORY_DEDUPE", "") == "1"


def _verify_run_all_token(provided: Optional[str]) -> None:
    """Gate /run-all and /cleanup on the shared secret. Fails closed: an unset
    RUN_ALL_TOKEN rejects all callers rather than leaving these public (the
    service is allUsers-invokable), since /cleanup is destructive."""
    expected = os.environ.get("RUN_ALL_TOKEN")
    if not expected:
        raise HTTPException(500, "RUN_ALL_TOKEN not configured")
    if not secrets.compare_digest(provided or "", expected):
        raise HTTPException(403, "invalid run-all token")


# --------------------------------------------------------------------------- #
# Auth for /run — verify a Google Identity ID token, then domain + allow-list.
# --------------------------------------------------------------------------- #
def verify_user(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer ID token")
    token = authorization.split(" ", 1)[1].strip()

    audience = os.environ.get("OAUTH_CLIENT_ID")
    if not audience:
        # Fail closed: without an audience, verify_oauth2_token skips audience
        # checks and would accept any valid Google ID token.
        raise HTTPException(500, "OAUTH_CLIENT_ID not configured")
    try:
        claims = google_id_token.verify_oauth2_token(token, ga_requests.Request(), audience)
    except Exception as exc:  # noqa: BLE001 — surface as 401
        raise HTTPException(401, f"invalid ID token: {exc}")

    email = (claims.get("email") or "").lower()
    if not claims.get("email_verified"):
        raise HTTPException(403, "email not verified")

    domain = os.environ.get("ALLOWED_DOMAIN", "").lower()
    if domain and not email.endswith("@" + domain):
        raise HTTPException(403, f"domain not allowed: {email}")

    allow = [e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()]
    if allow and email not in allow:
        raise HTTPException(403, f"not on allow-list: {email}")

    return email


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class RunRequest(BaseModel):
    client: str
    report: str
    during: Optional[str] = None
    locations: Optional[List[str]] = None  # subset; empty/None/"all" => all


class RunResponse(BaseModel):
    url: str
    title: str
    client: str
    report: str
    during: str
    locations: List[str]


def _to_response(r: RunResult) -> RunResponse:
    return RunResponse(
        url=r.url, title=r.title,
        client=r.client_key, report=r.report, during=r.during, locations=r.locations,
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    # Note: /healthz is intercepted by Google's frontend on Cloud Run, so use /health.
    return {"ok": True, "clients": sorted(load_clients()), "reports": REPORTS}


@app.get("/options")
def options():
    """Feed the Squarespace dropdowns."""
    clients = {k: {"name": c.name, "locations": c.locations} for k, c in load_clients().items()}
    return {"clients": clients, "reports": REPORTS, "during": queries.DURING_CHOICES}


@app.post("/run", response_model=RunResponse)
def run(req: RunRequest, user: str = Depends(verify_user)):
    if req.report not in queries.REPORTS:
        raise HTTPException(400, f"unknown report {req.report!r}")
    if req.during and req.during not in queries.DURING_CHOICES:
        raise HTTPException(400, f"unknown during {req.during!r}")
    try:
        result = run_report(
            req.client, req.report,
            folder_id=_folder_id(), during=req.during, locations=req.locations, dedupe=_dedupe(),
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc))
    log.info("run by %s -> %s", user, result.url)
    return _to_response(result)


# --------------------------------------------------------------------------- #
# One-time OAuth capture of each client store's offline token (cross-org).
# --------------------------------------------------------------------------- #
_oauth_state: Dict[str, str] = {}  # shop domain -> CSRF nonce (in-memory, one-shot)


def _redirect_uri(request: Request) -> str:
    # Derive from the incoming request so it works on any host (Cloud Run URL or
    # custom domain) with no env coupling. APP_BASE_URL overrides if set.
    base = os.environ.get("APP_BASE_URL") or str(request.base_url)
    return base.rstrip("/") + "/oauth/callback"


@app.get("/oauth/install")
def oauth_install(client: str, request: Request):
    """Start the install for one client; sends you to Shopify's consent screen."""
    try:
        c = get_client(client)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    state = secrets.token_urlsafe(24)
    _oauth_state[c.domain.lower()] = state
    return RedirectResponse(oauth.authorize_url(c, _redirect_uri(request), state))


@app.get("/oauth/callback", response_class=HTMLResponse)
def oauth_callback(request: Request):
    query = dict(request.query_params)
    shop = (query.get("shop") or "").lower()
    code = query.get("code")
    if not shop or not code:
        raise HTTPException(400, "missing shop or code")

    try:
        c = get_client_by_domain(shop)
    except KeyError as exc:
        raise HTTPException(404, str(exc))

    if _oauth_state.pop(shop, None) != query.get("state"):
        raise HTTPException(403, "state mismatch")
    if not oauth.verify_hmac(query, c):
        raise HTTPException(403, "HMAC verification failed")

    token = oauth.exchange_code(c, code)
    stored = store_token(c, token)
    log.info("captured offline token for %s (stored=%s)", c.key, stored)

    if stored:
        return f"<h3>✅ Token captured for {c.name} and saved to Secret Manager.</h3>"
    return (
        f"<h3>✅ Token captured for {c.name}.</h3>"
        "<p>No GCP project configured, so paste this into your .env as "
        f"<code>SHOPIFY_TOKEN_{c.key.upper()}</code>:</p>"
        f"<pre>{token}</pre>"
    )


@app.post("/cleanup")
def cleanup(x_run_all_token: Optional[str] = Header(None)):
    """Permanently delete ALL report sheets in the Shared Drive. Gated by the
    same shared secret as /run-all. Use to clear test runs / start fresh."""
    _verify_run_all_token(x_run_all_token)
    deleted = SheetsWriter().delete_reports(_folder_id())
    log.info("cleanup deleted %d sheets", deleted)
    return {"deleted": deleted}


@app.post("/run-all")
def run_all(x_run_all_token: Optional[str] = Header(None)):
    """Monthly batch: every client × both reports, all locations. Per-report DURING
    defaults (sales=last_month, inventory=today). The service is publicly invokable
    (so the browser can reach /run), so /run-all is gated by a shared secret that
    only Cloud Scheduler sends."""
    _verify_run_all_token(x_run_all_token)

    folder = _folder_id()
    writer = SheetsWriter()  # reuse one authenticated client across the batch
    session = requests.Session()
    dedupe = _dedupe()

    results: List[RunResult] = []
    failures: List[str] = []
    for client_key in load_clients():
        for report in REPORTS:
            try:
                results.append(run_report(
                    client_key, report,
                    folder_id=folder, dedupe=dedupe, writer=writer, session=session,
                ))
            except Exception as exc:  # noqa: BLE001 — isolate per report
                log.exception("run-all failed: %s/%s", client_key, report)
                failures.append(f"{client_key}/{report}: {exc}")

    notify.send_summary(results, failures)
    return {
        "ok": not failures,
        "sheets": [{"client": r.client_key, "report": r.report, "url": r.url} for r in results],
        "failures": failures,
    }
