# Abilium ETLs — Cloud Service

Turns the two monthly ETL scripts (sales-by-location, inventory-by-location) into
a Cloud Run service that pulls live from Shopify via ShopifyQL, writes a fresh
Google Sheet with pie charts to a Shared Drive, runs monthly, and is triggerable
from a domain-restricted Squarespace UI. Design notes: [CLOUD_MIGRATION_PLAN.md](CLOUD_MIGRATION_PLAN.md).
Day-2 operations: [RUNBOOK.md](RUNBOOK.md).

## Live deployment

| | |
|---|---|
| GCP project | `abilium-etls` (region `us-central1`) |
| Service URL | `https://abilium-etl-zbhofw3ooa-uc.a.run.app` |
| Runtime SA | `abilium-etl-run@abilium-etls.iam.gserviceaccount.com` |
| Image repo | `us-central1-docker.pkg.dev/abilium-etls/abilium/app:<tag>` |
| Clients | Enlightened Baby, Kindred Spirits, Kick Pleat |

Status: **deployed and live.** Remaining go-live gate is each store owner installing
their Shopify app; then capture the token (`/oauth/install?client=<key>`). The live
ShopifyQL `inventory_by_location` pull is validated on that first capture (plan phase 0).

## Layout

```
app.py                  FastAPI: /run (UI), /run-all (Scheduler), /oauth/*, /health, /options
core/
  config.py             clients.json loader + Secret Manager resolution
  queries.py            ShopifyQL templates (tabular, always location-filtered)
  shopify_client.py     run shopifyqlQuery -> DataFrame (field -> CSV header)
  transforms.py         SalesData / InventoryData ported to DataFrame-in
  sheets_writer.py      new Sheet in Shared Drive + pie charts
  oauth.py              cross-org OAuth offline-token capture
  runner.py             one report end-to-end
  notify.py             monthly summary email (Gmail SMTP)
config/clients.json     client registry (EB, KS, KP)
infra/                  Terraform (Cloud Run, secrets, scheduler, IAM) + Makefile
Dockerfile
```

`config/clients.json` and all app code are **baked into the container image** — changing
either requires a rebuild (see below). `infra/terraform.tfvars` and Secret Manager values
are infrastructure and do **not** require a rebuild.

## Local dev

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m tests.smoke          # transforms + query builder, no GCP/Shopify needed
```

## Deploying new code

Code or `config/clients.json` changes need a new image + a Terraform apply that points
Cloud Run at it. From the repo root:

```bash
# 0. (if gcloud/ADC has lapsed — org enforces periodic reauth)
gcloud auth login && gcloud auth application-default login

# 1. test
python -m tests.smoke

# 2. build & push a new image (pick the next tag, e.g. v17)
gcloud builds submit . --tag us-central1-docker.pkg.dev/abilium-etls/abilium/app:v17 --project abilium-etls

# 3. deploy that image
cd infra && terraform apply -var image_tag=v17

# 4. verify
curl https://abilium-etl-zbhofw3ooa-uc.a.run.app/health
```

Equivalent via the Makefile (from `infra/`): `make build TAG=v17 && make deploy TAG=v17`.

- **Config-only change** (e.g. an env var in `terraform.tfvars`): no rebuild — just
  `terraform apply -var image_tag=<current tag>`.
- **Rollback**: `terraform apply -var image_tag=<previous tag>` (old images stay in
  Artifact Registry).

## Adding a new client

Say the new client key is `new_client` with store `xyz.myshopify.com`.

**Shopify (Dev Dashboard, in the agency org):**
1. Create an app `abiliumETLsNewClient`.
2. Versions → scopes `read_reports`, Redirect URL `https://abilium-etl-zbhofw3ooa-uc.a.run.app/oauth/callback` → **Release**.
3. Partners → **API access requests** → Protected customer data → **Level 1**.
4. Partners → **Distribution** → Custom distribution → enter `xyz.myshopify.com` → generate link → **store owner installs it**.
5. Copy the app's **Client ID** and **Client Secret**.

**Code / config:**
6. Add to `config/clients.json`:
   ```json
   "new_client": {
     "name": "New Client",
     "domain": "xyz.myshopify.com",
     "api_version": "2026-04",
     "token_secret": "shopify-token-new-client",
     "locations": ["Location A", "Location B"]
   }
   ```
7. Add to `infra/main.tf` → `locals.token_secrets`:
   ```hcl
   new_client = "shopify-token-new-client"
   ```
   (This auto-creates both the token secret **and** the `new_client-oauth-client-secret`
   container, with IAM, and wires the `SHOPIFY_CLIENT_ID_NEW_CLIENT` env var.)
8. Add the Client ID to `infra/terraform.tfvars` → `shopify_client_ids`:
   ```hcl
   new_client = "<client-id>"
   ```

**Deploy + secrets + token:**
9. Rebuild (clients.json changed) and deploy:
   ```bash
   gcloud builds submit . --tag us-central1-docker.pkg.dev/abilium-etls/abilium/app:v17 --project abilium-etls
   cd infra && terraform apply -var image_tag=v17   # creates the new secret containers
   ```
10. Load the Client Secret:
    ```bash
    printf '%s' '<client-secret>' | gcloud secrets versions add new_client-oauth-client-secret --data-file=- --project abilium-etls
    ```
11. After the owner installs, capture the token:
    `https://abilium-etl-zbhofw3ooa-uc.a.run.app/oauth/install?client=new_client`
12. Done — the client now appears in the UI dropdown and the monthly run.

Naming must stay consistent: `token_secret` uses hyphens (`shopify-token-new-client`),
the OAuth-secret container uses the key with an underscore (`new_client-oauth-client-secret`),
the env var is upper-cased (`SHOPIFY_CLIENT_ID_NEW_CLIENT`). Steps 7–8 keep these in sync.

## 🗑️  BULK-DELETE ALL REPORT SHEETS

Every run creates a fresh sheet, so the Shared Drive accumulates them. To wipe
**all** report sheets and start clean, POST to `/cleanup` with the run-all token
(the runtime service account does the deletion — no local Drive auth needed):

```bash
TOKEN=$(awk -F'"' '/run_all_token/{print $2}' infra/terraform.tfvars)
curl -X POST https://abilium-etl-zbhofw3ooa-uc.a.run.app/cleanup -H "X-Run-All-Token: $TOKEN"
# -> {"deleted": 42}
```

⚠️ This **trashes every** report sheet in the output Shared Drive (not just test
runs) — they leave the Drive view and the Shared Drive trash auto-purges after 30
days (a Manager can empty it sooner). They regenerate on the next `/run` or monthly
`/run-all`, so it's safe to clear anytime.

## Endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /health`, `GET /options` | none | liveness; dropdown data |
| `POST /run` | Google ID token (domain + allow-list) | one report from the UI |
| `POST /run-all` | `X-Run-All-Token` header | monthly batch (Scheduler) |
| `POST /cleanup` | `X-Run-All-Token` header | delete all report sheets (see above) |
| `GET /oauth/install?client=<key>` → `/oauth/callback` | Shopify HMAC + state | capture offline token |
