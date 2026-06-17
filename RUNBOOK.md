# Runbook — Abilium ETL

Day-2 operations. For deploying code or adding a client, see [README.md](README.md).

## Quick reference

| | |
|---|---|
| Project / region | `abilium-etls` / `us-central1` |
| Service URL | `https://abilium-etl-zbhofw3ooa-uc.a.run.app` |
| Runtime SA | `abilium-etl-run@abilium-etls.iam.gserviceaccount.com` |
| Scheduler job | `abilium-etl-monthly` (1st @ 04:00 America/Chicago) |
| Secrets | `shopify-token-<client>` (offline token), `<key>-oauth-client-secret`, `smtp-password` |
| `run_all_token` | in `infra/terraform.tfvars` (sent as `X-Run-All-Token`) |

## Before any gcloud/terraform command — reauth

The Workspace org enforces periodic reauth, so local gcloud/ADC lapses (often daily).
If a command errors with *"Reauthentication failed"*:

```bash
gcloud auth login
gcloud auth application-default login   # consent to ALL scopes
```
This never affects production — Cloud Run runs as the runtime SA, which doesn't reauth.

## Capture / refresh a store token

Requires the store **owner** to have installed that store's Shopify app.

```
# open in a browser:
https://abilium-etl-zbhofw3ooa-uc.a.run.app/oauth/install?client=<key>
```
Approve the Shopify consent → callback stores the offline token. Verify:
```bash
gcloud secrets versions list shopify-token-<client> --project abilium-etls
```
Offline tokens don't expire. Re-running `/oauth/install` adds a new version (rotation).
`<key>` is the clients.json key (`enlightened_baby`, `kindred_spirits`, `kick_pleat`);
the secret name uses hyphens (`shopify-token-enlightened-baby`).

## Run a report

- **One report:** use the Squarespace UI (it mints the required Google ID token via sign-in).
- **Full monthly batch on demand** (creates all Sheets + sends the summary email):
  ```bash
  TOKEN=$(awk -F'"' '/run_all_token/{print $2}' infra/terraform.tfvars)
  curl -X POST https://abilium-etl-zbhofw3ooa-uc.a.run.app/run-all -H "X-Run-All-Token: $TOKEN"
  ```
- **Local single-report debug:** put a captured token in `.env` as `SHOPIFY_TOKEN_<KEY>`,
  set `DRIVE_FOLDER_ID`, `gcloud auth application-default login`, then:
  ```python
  from core.runner import run_report
  print(run_report("kick_pleat", "inventory", folder_id="0APv--Bb6l4qzUk9PVA"))
  ```

## Logs

```bash
gcloud run services logs read abilium-etl --region us-central1 --project abilium-etls --limit 50
# narrow:  ... | grep -iE "ERROR|summary|run-all"
```

## Test / re-send the monthly summary email

Trigger `/run-all` as above. It always emails a summary (links on success, failures otherwise).
Look for `INFO:notify:summary emailed to ...` in the logs to confirm delivery.

## Rotate a secret

- **Shopify Client Secret:** `printf '%s' '<new>' | gcloud secrets versions add <key>-oauth-client-secret --data-file=- --project abilium-etls`. Read at request time → effective immediately (no redeploy).
- **Store offline token:** re-run `/oauth/install?client=<key>` (adds a new version). Effective immediately.
- **SMTP App Password:** `... gcloud secrets versions add smtp-password ...`. This one is **mounted as an env var** (resolved at instance start), so redeploy to pick it up: `cd infra && terraform apply -var image_tag=<current tag>`.
- **`run_all_token`:** edit `infra/terraform.tfvars` → `terraform apply -var image_tag=<current tag>` (updates both the Cloud Run env and the Scheduler header together).

## Pause / resume the monthly job

```bash
gcloud scheduler jobs pause  abilium-etl-monthly --location us-central1 --project abilium-etls
gcloud scheduler jobs resume abilium-etl-monthly --location us-central1 --project abilium-etls
gcloud scheduler jobs run    abilium-etl-monthly --location us-central1 --project abilium-etls   # fire now
```

## Common errors

| Symptom | Cause / fix |
|---|---|
| `Secret shopify-token-… not found or has no versions` | Token not captured yet — owner installs app, then `/oauth/install`. |
| `/run` → 401 | Missing/expired Google ID token — sign in again. |
| `/run` → 403 | Email domain not `abiliumtheagency.com`, or not on `ALLOWED_EMAILS`. |
| `/run-all` → 403 | Wrong/missing `X-Run-All-Token`. |
| OAuth callback → `state mismatch` / `HMAC` | Stale install link, or wrong Client Secret loaded for that client. |
| ShopifyQL `parse errors` on inventory | `inventory_by_location` dataset issue — see plan phase-0 note. |
| `Reauthentication failed` (local) | `gcloud auth login` + `application-default login`. |

## Redeploy / rollback

```bash
cd infra
terraform apply -var image_tag=<tag>          # deploy a specific image
terraform apply -var image_tag=<previous>     # rollback (old images persist in Artifact Registry)
```
