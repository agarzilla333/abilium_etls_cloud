# Abilium ETLs — Cloud Migration Plan

Turn the two monthly ETL scripts into cloud services that pull live from Shopify,
write to Google Sheets with pie charts, run on a schedule, and are triggerable from
a password-protected Squarespace UI.

## Decisions locked

| Area | Decision |
|---|---|
| Cloud platform | **Google Cloud** — Cloud Run + Cloud Scheduler + Secret Manager |
| Shopify data source | **`shopifyqlQuery`** in the GraphQL Admin API (run existing ShopifyQL verbatim) |
| Clients | Separate Shopify store per client; **one client has multiple stores** |
| Multi-location output | **One combined sheet**, aggregated **in the query** via `WHERE inventory_location_name IN (...)` |
| Output | **Fresh Google Sheet each run** with pie charts |
| Sheet storage | **Shared Drive** folder. Created by a human/admin (service accounts can't create Shared Drives); runtime SA added as a member (Content Manager) and writes sheets into it. |
| Monthly delivery | **Email me the links** after the run |
| UI auth | **Google sign-in restricted to our domain** |
| Schedule | 1st of each month, **04:00**, all clients, both reports |
| `DURING` per report | **Sales = `last_month`; Inventory = `today`** (inventory is a point-in-time snapshot, not a period — see Open risks) |
| Store/location model | **One Shopify store + token per client**, with a list of `inventory_location_name`s (Kick Pleat = Austin, Dallas, Houston). The **UI offers a location selector**; **default and the monthly cron run all of a client's locations.** Aggregation is the query's `WHERE … IN (...)` filter — no per-store concat needed. |

## Open risks — validate these FIRST (before any build)

Verified (2026-06): `shopifyqlQuery` is **live in the latest Admin API (2026-04)**, not
deprecated. `FROM sales` is confirmed supported via the API. The rest of these are the
load-bearing unknowns; resolve them with a throwaway live query before scaffolding anything.

1. **Is `FROM inventory_by_location` available via `shopifyqlQuery`?** ❗ Highest risk.
   Shopify's API docs enumerate `sales`, `sessions`, `customers`, `ORGANIZATION sales` — and
   do **not** clearly list inventory. If inventory isn't an API-exposed ShopifyQL dataset, the
   inventory report cannot use the verbatim-query approach and must be rebuilt from
   `InventoryLevel`/`InventoryItem` GraphQL objects (substantially more work). **Smoke-test the
   inventory query against a live store before committing to this design.**
2. **`shopifyqlQuery` scope is bigger than stated.** It needs `read_reports` **plus Level-2
   protected customer data access** (an app-approval step), not just "read analytics/inventory."
3. **Inventory `DURING`:** ending inventory is point-in-time (`DURING today`), so it cannot run
   `last_month` like sales. Per-report `DURING` is now in the decisions table.
4. ~~Store-vs-location model~~ **Resolved:** one store/token per client + a location list; the UI
   selects locations, default/cron = all; aggregation via the query's `WHERE … IN` filter.

## Key insight: ShopifyQL runs *near*-verbatim

The queries already written in the script comments are ShopifyQL. Shopify exposes
`shopifyqlQuery` in the GraphQL Admin API, so we run essentially the same queries against the
API and get back the same `Net sales` / `Gross sales` / `Ending inventory ... (at location)`
data currently exported by hand. The pandas transforms keep their logic — but "verbatim" has
three asterisks that are real work, not free:

- **Strip UI-only clauses.** `VISUALIZE …` and the `WITH TOTALS` rollup are analytics-UI
  constructs. The API returns `tableData`, so each query is rewritten to plain
  `SHOW … GROUP BY …` tabular form (and we decide what to do with the totals row).
- **Map column names.** The scripts read the analytics-UI CSV headers (`"Net sales"`,
  `"Product vendor"`, `"Ending inventory units (at location)"`, …). The API returns the
  ShopifyQL *field* names (`net_sales`, `product_vendor`, `ending_inventory_units_at_location`).
  `shopify_client` must map field→header so the transforms run unchanged. One wrong header
  string = a pandas `KeyError`.
- **Preserve `GROUP BY` granularity.** The sales transform counts `units = 1 per row`, which
  only holds if the API query's `GROUP BY` reproduces the CSV's line-item grouping exactly.

`DURING` (`this_month`, `last_month`, `today`, ...) is native ShopifyQL — a string injected
into the query, mapping 1:1 to the UI dropdown — **but it differs per report** (see decisions).

**Caveat:** Shopify renames returns fields to `sales_reversals` (old names **removed in API
version 2026-07**; reachable on older versions until 2027-04). Pin **one** explicit API
version and match field names to it — you can't use the new names *and* an older "current"
version at once.

## Architecture

```
Squarespace page (password-protected)
  └─ embedded HTML/JS: Google sign-in + dropdowns (client, report, DURING, locations)
        │  POST {client, report, during, locations[]} + Google ID token
        ▼  (locations omitted / "all" ⇒ all of the client's locations)
Cloud Run service  (FastAPI)
  ├─ /run        ← UI: verify Google token (domain-restricted) → run one report for chosen locations
  └─ /run-all    ← Cloud Scheduler: loop all clients × both reports, all locations
                   (sales DURING=last_month, inventory DURING=today)
        │
        ▼  shared core
  shopify_client.run_shopifyql(store, query)   → DataFrame   (per store)
  transforms.sales_by_location(df) / inventory(df)          (existing pandas logic)
  sheets_writer.write(df_dict) → new Google Sheet + pie charts → URL
        │
        ├─ Secret Manager   (Shopify tokens per store)
        ├─ Shared Drive      (output sheets)
        └─ Email (links)     (after /run-all)
```

## Components to build

1. **`config/clients.json`** — client registry. Each client → a domain, an API version, a
   Secret Manager ref for its **single token**, and a list of its **`inventory_location_name`s**.
   The UI may select a subset; an omitted/"all" selection — and every cron run — uses the full
   list. Aggregation is just the query filter. Known roster:

   | Client | `inventory_location_name`(s) |
   |---|---|
   | Enlightened Baby | `Austin Store` |
   | Kindred Spirits | `Shop location` |
   | Kick Pleat | `Kick Pleat Austin`, `Kick Pleat Dallas`, `Kick Pleat Houston` |

   Single-location clients still go through the same `WHERE inventory_location_name IN (...)`
   path (a list of one) — no special-casing.

2. **`core/shopify_client.py`** — runs a ShopifyQL string via `shopifyqlQuery`, paginates,
   and **maps the returned ShopifyQL field names to the analytics-UI CSV headers** the
   transforms expect, returning a tidy DataFrame.

3. **`core/queries.py`** — the two ShopifyQL templates (sales-by-location, inventory-by-location)
   parameterized by `DURING` (per report) and the location-name list. **Both templates always
   include the `WHERE inventory_location_name IN (...)` clause** built from the client's locations
   — same pattern for sales and inventory; the old unfiltered sales variant is dropped. **Rewritten
   to tabular `SHOW … GROUP BY …`** — `VISUALIZE`/`WITH TOTALS` removed — and with `GROUP BY`
   granularity preserved exactly so `units = 1 per row` still holds in the sales transform.

4. **`core/transforms.py`** — your existing `SalesData` / `InventoryData` logic, refactored to
   take a DataFrame in and return the dict/df structures (vendor / product / channel; inventory
   granular). Multi-location aggregation already happened in the query's `WHERE … IN` filter, so
   the transform groups one combined DataFrame as today — no concat step. **Decision needed:** the
   inventory SKU/heuristic dedupe is currently
   **commented out** in the source script (retail values are a known over-count / upper bound) —
   ship as-is (dedupe off) or finish + enable it before migrating. Don't carry it silently.

5. **`core/sheets_writer.py`** — creates a new Sheet in the Shared Drive, writes tabs (mirroring
   today's Excel tabs), and adds **pie charts** via the Sheets API `batchUpdate` (EmbeddedChart,
   `PIE`): total sales by vendor, sales by product type, units by channel (sales report);
   retail value by designer, by product type (inventory report). Returns the Sheet URL.

6. **`app.py`** — FastAPI with `/run` and `/run-all`; token verification; email send. `/run`
   accepts `{client, report, during, locations[]}`; an empty/"all" `locations` resolves to the
   client's full list from `clients.json`.

7. **Squarespace embed** — code block: Google Identity Services sign-in button + dropdowns
   (client, report, `DURING`, and a **location selector** — multi-select with an "All" default);
   on submit, sends the ID token + selections to `/run`, shows the returned Sheet link.

## Reports (unchanged outputs, live inputs)

- **Sales by location** → tabs *By Vendor*, *By Product*, *By Channel* (Units, Net Sales).
  Vendor backfill heuristic (mode-by-title, then regex) is preserved.
- **Inventory by location** → tabs *inventory* (by designer), *by product*, and one tab per
  designer (granular). Dedupe is currently **disabled** in the source (over-counts; see the
  transforms decision). Designer tab names are truncated to 31 chars — watch for collisions in
  Google Sheets (no duplicate tab names allowed).

## Security

- **Two distinct auth layers — don't conflate them.** The browser calls `/run` with a Google
  **Identity ID token** (app-level), which is *not* a Cloud Run IAM identity. So `/run` must be
  **IAM-invokable by `allUsers`** and do its own in-app verification (valid ID token → email
  domain matches our domain → address on the allow-list). Only `/run-all` stays IAM-restricted,
  invoked by Cloud Scheduler's OIDC SA. (Earlier "no public unauth invoker" applied blanket-wide
  was self-contradictory with the browser-direct design.) Alternatively, front `/run` with API
  Gateway/IAP. Either way, `/run` needs **CORS** for the Squarespace origin.
- **No exported SA key.** Cloud Run already *runs as* a service account — share the Shared Drive
  with that runtime SA and use Application Default Credentials. Don't store an exported Sheets SA
  JSON key in Secret Manager (long-lived credential to leak/rotate); this removes a secret.
- Shopify tokens live in **Secret Manager**; nothing sensitive is in the Squarespace page or repo.
- **Shopify auth is cross-org (agency).** Legacy in-admin custom apps were retired 2026-01-01, so
  each client store gets its **own Dev Dashboard app**, distributed via **custom distribution**
  (one store per app). The app's org (Abilium the Agency) differs from each client store's org, so
  `client_credentials` is unusable (same-org only, and 24h tokens). We use the **authorization-code
  grant** to capture a **permanent offline token** once per store — `GET /oauth/install?client=<key>`
  → Shopify consent → `/oauth/callback` exchanges the code, stores the offline token in Secret
  Manager. The runtime sends it as `X-Shopify-Access-Token`. Scope: `read_reports` **+ Level-2
  protected customer data access** (required by `shopifyqlQuery`). The OAuth redirect URL
  (`APP_BASE_URL/oauth/callback`) must be registered in each app's allowed redirection URLs.

## Schedule

Cloud Scheduler → `POST /run-all` at `0 4 1 * *` (America/Chicago). Service loops all clients and
both reports — **sales with `DURING=last_month`, inventory with `DURING=today`** — creates sheets,
then emails one summary.

- **Partial-failure isolation:** wrap each client×report in try/except so one failed store doesn't
  abort the batch. The summary email reports **failures as well as links**.
- **Cloud Run timeout:** `/run-all` loops everything in one request; a live pull + pagination +
  Sheets `batchUpdate` per report can exceed the 5-min default (60-min max). Raise the timeout,
  and for the synchronous browser `/run`, consider returning `202` + emailing the link rather than
  holding the page open if a single report runs long.
- **Email mechanism** (unspecified): pick one — SMTP, SendGrid, or Gmail API (needs domain-wide
  delegation). It affects IAM/secrets.

## Cost

Cloud Run (scale-to-zero) + Scheduler + Secret Manager at this volume ≈ **$0–2/month**.

## Build phases

0. **De-risk first (one throwaway live query, no infra):** confirm `shopifyqlQuery` returns
   **`FROM inventory_by_location`** data, then `FROM sales`. If inventory isn't API-exposed,
   redesign the inventory path now (GraphQL inventory objects) before anything else.
1. Commit the two existing scripts into this repo (they live in `../abilium_etls` today).
   GCP project, Shared Drive (human-created, SA added), service account, Secret Manager; load one
   store's token.
2. `shopify_client` (+ field→header mapping) + `queries` (tabular, `VISUALIZE`/`WITH TOTALS`
   stripped): confirm a live pull matches a known CSV export — **inventory and sales separately.**
3. Port transforms to DataFrame-in; verify numbers match current Excel for one client (this gate
   catches `GROUP BY`/units drift). Settle the inventory dedupe decision here.
4. `sheets_writer` with pie charts; verify a full sheet end-to-end.
5. `app.py` `/run` (in-app token verify + CORS); deploy to Cloud Run; test with a token.
6. Squarespace embed + Google sign-in wired to `/run`.
7. `/run-all` + Cloud Scheduler + email (per-report `DURING`, partial-failure isolation);
   dry-run, then enable.

## Infrastructure as Code (IaC)

Everything in GCP is defined in **Terraform** and applied from the CLI, so the whole stack is
reproducible, reviewable in git, and tear-down-able. App **code** ships as a container built by
Cloud Build; Terraform manages the **infrastructure** and points Cloud Run at the latest image.

### Tooling

- **Terraform** (`google` + `google-beta` providers) — all GCP resources.
- **Remote state** in a GCS bucket with versioning + state locking (created once via a small bootstrap).
- **Cloud Build** builds/pushes the container to **Artifact Registry**; Terraform deploys it.
- One Terraform **workspace per environment** (`dev`, `prod`) so the same code stands up isolated stacks.

### Resources Terraform manages

- Project services (APIs): Cloud Run, Cloud Build, Cloud Scheduler, Secret Manager, Artifact Registry,
  IAM, Drive/Sheets are app-level (enabled via APIs).
- **Artifact Registry** Docker repo.
- **Cloud Run** service (the FastAPI app), with env vars and the runtime service account.
- **Service accounts** + IAM: a runtime SA (Cloud Run) and a deploy SA (CI); least-privilege bindings.
- **Secret Manager** secrets (one per Shopify store token; **no Sheets SA key** — use the runtime
  SA's identity + Shared Drive membership instead) — secret *containers* in Terraform; secret
  **values** loaded out-of-band so they never touch git/state.
- **Cloud Scheduler** job → `POST /run-all` at `0 4 1 * *` (America/Chicago), authenticated with an
  OIDC token from the scheduler SA.
- IAM allowing Scheduler to invoke Cloud Run; Cloud Run access restricted (no public unauth invoker).

### Repo layout

```
infra/
  bootstrap/            # one-time: GCS state bucket + deploy SA (run locally, then commit state config)
  modules/
    cloud_run/          # service, IAM, env
    scheduler/          # monthly job + invoker IAM
    secrets/            # secret containers (values loaded separately)
    artifact_registry/
  envs/
    dev/   main.tf  terraform.tfvars   backend.tf
    prod/  main.tf  terraform.tfvars   backend.tf
  Makefile              # thin wrappers around the commands below
```

### CLI workflow

```bash
# one-time bootstrap (creates the remote-state bucket + deploy SA)
cd infra/bootstrap && terraform init && terraform apply

# build & push the app image (Cloud Build), tag = git SHA
gcloud builds submit --tag $REGION-docker.pkg.dev/$PROJECT/abilium/app:$(git rev-parse --short HEAD)

# stand up / update infrastructure for an environment
cd infra/envs/prod
terraform init
terraform plan  -var image_tag=$(git rev-parse --short HEAD)   # preview
terraform apply -var image_tag=$(git rev-parse --short HEAD)   # deploy

# load/rotate a secret value (kept out of git & state)
echo -n "$SHOPIFY_TOKEN" | gcloud secrets versions add shopify-token-<store> --data-file=-

# tear everything down
terraform destroy
```

A `Makefile` collapses these to `make build`, `make plan`, `make deploy`, `make destroy` so a
single command from the CLI updates all GCP services. Later, the same `plan`/`apply` can run from
CI on push to `main` for hands-off deploys.

### Boundaries

- Terraform owns infra and which image tag Cloud Run runs; it does **not** store secret values
  or build app code.
- Secret values and the per-store Shopify tokens are loaded via `gcloud secrets versions add`
  (or pulled from your password manager), never committed.
- `clients.json` is app config (lives with the code), not infrastructure.

## What I'll need from you

- A GCP project (or let me propose names and you create it).
- Shopify **custom-app Admin API tokens** per store, with `read_reports` **+ Level-2 protected
  customer data access** approved (required by `shopifyqlQuery`).
- A **Shared Drive** you create and share with the runtime service account (SAs can't create one).
- Your business **email domain** for the sign-in restriction + the email address for run summaries.
- Squarespace access to add a code block on the protected page (or I give you paste-ready HTML).
- Location names captured (EB = `Austin Store`, KS = `Shop location`, KP = the three above);
  confirm there are no other clients/locations. (If any client *does* span genuinely separate
  Shopify stores, flag it — that's the one case needing a second token.)
- A quick **live `shopifyqlQuery` test** against one store so we can confirm the inventory dataset
  is API-exposed before building (phase 0).
