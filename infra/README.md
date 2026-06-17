# Infra (Terraform) — Abilium ETL

Single flat stack (plus a one-time bootstrap for remote state). Manages: required
APIs, Artifact Registry, the Cloud Run service + runtime SA, Secret Manager
containers (token per client + OAuth client-secret per client) with IAM, public
invoker, and the monthly Cloud Scheduler job.

Secret **values** are never in Terraform/state: client tokens are written by the
app's `/oauth/callback`; OAuth client secrets + the run-all token are loaded via
`gcloud secrets versions add`.

## One-time bootstrap (remote state)

```bash
cd infra/bootstrap
terraform init
terraform apply -var project=<PROJECT> -var state_bucket=<UNIQUE-BUCKET-NAME>
```
Put that bucket name in `infra/backend.tf` (`bucket = ...`).

## Stand up the stack

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars   # fill in values
terraform init

# 1) create Artifact Registry + enable APIs so there's somewhere to push
make repo

# 2) build & push the image (tag = git short SHA)
make build

# 3) create/update everything
make deploy            # = terraform apply -var image_tag=$(git rev-parse --short HEAD)
```

`terraform output service_url` → the Cloud Run URL.

## Load secret values (out-of-band)

```bash
# OAuth client secrets (from each Dev Dashboard app's API credentials)
printf '%s' "$EB_CLIENT_SECRET" | gcloud secrets versions add enlightened_baby-oauth-client-secret --data-file=-
printf '%s' "$KS_CLIENT_SECRET" | gcloud secrets versions add kindred_spirits-oauth-client-secret --data-file=-
printf '%s' "$KP_CLIENT_SECRET" | gcloud secrets versions add kick_pleat-oauth-client-secret --data-file=-

# Shopify offline tokens are written automatically by /oauth/callback,
# but you can seed/rotate one manually too:
# printf '%s' "$TOKEN" | gcloud secrets versions add shopify-token-kick-pleat --data-file=-
```

## Wire up Shopify + Drive, then capture tokens

1. In each Shopify app: register `<service_url>/oauth/callback` as an allowed
   redirect URL; scope = `read_reports` (+ protected customer data); release.
2. Share the Shared Drive with the runtime SA (`terraform output
   runtime_service_account`) as **Content Manager**.
3. Per client, visit `<service_url>/oauth/install?client=<key>` (after the owner
   has installed the app on that store) → token lands in Secret Manager.
4. Smoke test: `<service_url>/healthz`, then a single `/run`.

## Teardown

```bash
make destroy
```
