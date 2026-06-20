locals {
  # client key -> Secret Manager secret id holding its offline token.
  # Must match config/clients.json `token_secret`.
  token_secrets = {
    enlightened_baby = "shopify-token-enlightened-baby"
    kindred_spirits  = "shopify-token-kindred-spirits"
    kick_pleat       = "shopify-token-kick-pleat"
  }

  # Secret Manager secret id holding each app's OAuth Client Secret.
  # Must match core/config.get_oauth_client_secret: "<key>-oauth-client-secret".
  oauth_secrets = { for k, _ in local.token_secrets : k => "${k}-oauth-client-secret" }

  # Non-token secret containers (Gmail SMTP App Password for the monthly summary).
  extra_secrets = ["smtp-password"]

  all_secrets = toset(concat(values(local.token_secrets), values(local.oauth_secrets), local.extra_secrets))

  image = "${var.region}-docker.pkg.dev/${var.project}/abilium/app:${var.image_tag}"

  # Non-secret env vars for Cloud Run. Tokens + OAuth client secrets are read
  # from Secret Manager at runtime (via GCP_PROJECT), not injected here.
  base_env = {
    GCP_PROJECT      = var.project
    DRIVE_FOLDER_ID  = var.drive_folder_id
    OAUTH_CLIENT_ID  = var.oauth_client_id
    ALLOWED_DOMAIN   = var.allowed_domain
    ALLOWED_EMAILS   = var.allowed_emails
    CORS_ORIGINS     = var.cors_origins
    SUMMARY_EMAIL    = var.summary_email
    INVENTORY_DEDUPE = var.inventory_dedupe ? "1" : "0"
    RUN_ALL_TOKEN    = var.run_all_token
    SMTP_HOST        = var.smtp_host
    SMTP_PORT        = var.smtp_port
    SMTP_USER        = var.smtp_user
    SMTP_FROM        = var.smtp_from != "" ? var.smtp_from : var.smtp_user
  }

  client_id_env = {
    for k, _ in local.token_secrets :
    "SHOPIFY_CLIENT_ID_${upper(k)}" => lookup(var.shopify_client_ids, k, "")
  }

  env_vars = merge(local.base_env, local.client_id_env)
}

# --- Enable required APIs ---
resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudscheduler.googleapis.com",
    "iam.googleapis.com",
    "drive.googleapis.com",
    "sheets.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# --- Artifact Registry (Docker images) ---
resource "google_artifact_registry_repository" "app" {
  repository_id = "abilium"
  location      = var.region
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}

# --- Service accounts ---
resource "google_service_account" "runtime" {
  account_id   = "${var.service_name}-run"
  display_name = "Abilium ETL Cloud Run runtime"
}

# --- Secret containers (values loaded out-of-band) ---
resource "google_secret_manager_secret" "secret" {
  for_each  = local.all_secrets
  secret_id = each.value
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# Runtime SA can READ every secret (tokens + oauth client secrets)...
resource "google_secret_manager_secret_iam_member" "accessor" {
  for_each  = google_secret_manager_secret.secret
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

# ...and can WRITE new token versions (the /oauth/callback captures tokens).
resource "google_secret_manager_secret_iam_member" "token_adder" {
  for_each  = local.token_secrets
  secret_id = google_secret_manager_secret.secret[each.value].id
  role      = "roles/secretmanager.secretVersionAdder"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

# --- Cloud Run service ---
resource "google_cloud_run_v2_service" "app" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.runtime.email
    timeout         = "900s" # /run-all loops all clients; allow up to 15 min

    scaling {
      max_instance_count = 2
    }

    containers {
      image = local.image
      ports {
        container_port = 8080
      }
      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }
      dynamic "env" {
        for_each = local.env_vars
        content {
          name  = env.key
          value = env.value
        }
      }
      # SMTP password from Secret Manager — only mounted once SMTP is enabled
      # (smtp_user set) and the secret has a value, so empty deploys don't fail.
      dynamic "env" {
        for_each = var.smtp_user != "" ? toset(["SMTP_PASSWORD"]) : toset([])
        content {
          name = env.value
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.secret["smtp-password"].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [google_project_service.apis]
}

# Public invoker so the browser can reach /run; /run-all is gated in-app by
# RUN_ALL_TOKEN, /oauth/* by state+HMAC, /run by the Google ID token.
resource "google_cloud_run_v2_service_iam_member" "public" {
  name     = google_cloud_run_v2_service.app.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# --- Monthly schedule -> POST /run-all ---
resource "google_cloud_scheduler_job" "monthly" {
  name             = "${var.service_name}-monthly"
  schedule         = "0 4 1 * *"
  time_zone        = "America/Chicago"
  attempt_deadline = "1800s"

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.app.uri}/run-all"
    headers = {
      "Content-Type"    = "application/json"
      "X-Run-All-Token" = var.run_all_token
    }
    body = base64encode("{}")
  }

  depends_on = [google_project_service.apis]
}
