"""Client registry + secret resolution.

`clients.json` is app config (lives with the code). Shopify tokens live in
Secret Manager and are resolved at runtime — never committed. For local dev you
can shadow a token with an env var ``SHOPIFY_TOKEN_<CLIENT_KEY_UPPER>`` so you
don't need GCP credentials to smoke-test transforms.
"""
from __future__ import annotations

import functools
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

# Load a local .env if present (no-op in Cloud Run, where env comes from the
# platform). Optional so prod doesn't depend on python-dotenv being installed.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "clients.json"

# OAuth scopes requested when installing the app on a client store.
SCOPES = os.environ.get("SHOPIFY_SCOPES", "read_reports")


def _project() -> Optional[str]:
    return os.environ.get("GCP_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")


@dataclass(frozen=True)
class Client:
    key: str
    name: str
    domain: str
    api_version: str
    token_secret: str
    locations: List[str]


@functools.lru_cache(maxsize=1)
def _raw() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_clients() -> Dict[str, Client]:
    out: Dict[str, Client] = {}
    for key, c in _raw()["clients"].items():
        out[key] = Client(
            key=key,
            name=c["name"],
            domain=c["domain"],
            api_version=c["api_version"],
            token_secret=c["token_secret"],
            locations=list(c["locations"]),
        )
    return out


def get_client(key: str) -> Client:
    clients = load_clients()
    if key not in clients:
        raise KeyError(f"unknown client {key!r}; known: {sorted(clients)}")
    return clients[key]


def resolve_locations(client: Client, requested: Optional[List[str]]) -> List[str]:
    """UI may pass a subset; empty / 'all' / None -> the client's full list.

    Cron always passes None, so the monthly run covers every location.
    """
    if not requested or requested == ["all"]:
        return list(client.locations)
    unknown = [loc for loc in requested if loc not in client.locations]
    if unknown:
        raise ValueError(f"{client.key}: unknown locations {unknown}; valid: {client.locations}")
    return list(requested)


def get_client_by_domain(domain: str) -> Client:
    domain = domain.strip().lower()
    for client in load_clients().values():
        if client.domain.lower() == domain:
            return client
    raise KeyError(f"no client configured for shop domain {domain!r}")


def get_token(client: Client) -> str:
    """Resolve the runtime Shopify token (a permanent OAuth offline token).

    Env override first (``SHOPIFY_TOKEN_<KEY>``), else Secret Manager.
    """
    env_key = f"SHOPIFY_TOKEN_{client.key.upper()}"
    if os.environ.get(env_key):
        return os.environ[env_key]

    project = _project()
    if not project:
        raise RuntimeError(
            f"no token for {client.key!r}: set {env_key} for local dev, "
            "or GCP_PROJECT to read from Secret Manager"
        )

    # Imported lazily so local/transform-only work doesn't require the GCP libs.
    from google.cloud import secretmanager

    sm = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project}/secrets/{client.token_secret}/versions/latest"
    return sm.access_secret_version(name=name).payload.data.decode("utf-8")


def get_oauth_client_id(client: Client) -> str:
    val = os.environ.get(f"SHOPIFY_CLIENT_ID_{client.key.upper()}")
    if not val:
        raise RuntimeError(f"set SHOPIFY_CLIENT_ID_{client.key.upper()} (the app's Client ID)")
    return val


def get_oauth_client_secret(client: Client) -> str:
    """The app's Client Secret — used only during OAuth token capture."""
    env_key = f"SHOPIFY_CLIENT_SECRET_{client.key.upper()}"
    if os.environ.get(env_key):
        return os.environ[env_key]

    project = _project()
    if not project:
        raise RuntimeError(f"set {env_key} for local dev, or GCP_PROJECT for Secret Manager")

    from google.cloud import secretmanager

    sm = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project}/secrets/{client.key}-oauth-client-secret/versions/latest"
    return sm.access_secret_version(name=name).payload.data.decode("utf-8")


def store_token(client: Client, token: str) -> bool:
    """Persist a captured offline token to Secret Manager. Returns True if stored,
    False if no GCP project is configured (caller should surface it for manual save)."""
    project = _project()
    if not project:
        return False

    from google.cloud import secretmanager

    sm = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{project}/secrets/{client.token_secret}"
    sm.add_secret_version(parent=parent, payload={"data": token.encode("utf-8")})
    return True
