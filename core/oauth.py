"""One-time OAuth capture of a permanent offline Admin API token per client store.

Cross-org setup: the app lives in the agency org; each client store is a separate
org reached via the app's *custom distribution* link. ``client_credentials`` won't
work across orgs, so we use the authorization-code grant, which yields a permanent
OFFLINE token (no 24h expiry). That token is what the runtime uses thereafter.

Flow:
  /oauth/install?client=<key>  -> 302 to Shopify's authorize screen
  Shopify  -> /oauth/callback?code&shop&hmac&state
  callback -> verify hmac + state -> exchange code -> store offline token
"""
from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlencode

import requests

from .config import (
    SCOPES,
    Client,
    get_oauth_client_id,
    get_oauth_client_secret,
)


def authorize_url(client: Client, redirect_uri: str, state: str) -> str:
    params = {
        "client_id": get_oauth_client_id(client),
        "scope": SCOPES,
        "redirect_uri": redirect_uri,
        "state": state,
        # offline token = default (omit grant_options[]=per-user)
    }
    return f"https://{client.domain}/admin/oauth/authorize?{urlencode(params)}"


def verify_hmac(query: dict, client: Client) -> bool:
    """Validate the callback HMAC against the app's client secret."""
    provided = query.get("hmac")
    if not provided:
        return False
    message = "&".join(
        f"{k}={query[k]}" for k in sorted(query) if k not in ("hmac", "signature")
    )
    digest = hmac.new(
        get_oauth_client_secret(client).encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(digest, provided)


def exchange_code(client: Client, code: str) -> str:
    """Exchange an authorization code for a permanent offline access token."""
    resp = requests.post(
        f"https://{client.domain}/admin/oauth/access_token",
        json={
            "client_id": get_oauth_client_id(client),
            "client_secret": get_oauth_client_secret(client),
            "code": code,
        },
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError(f"no access_token in exchange response: {resp.text}")
    return token
