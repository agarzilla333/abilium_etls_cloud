variable "project" {
  type        = string
  description = "GCP project id."
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "image_tag" {
  type        = string
  description = "Container image tag to deploy (usually the git short SHA)."
}

variable "service_name" {
  type    = string
  default = "abilium-etl"
}

# --- App configuration (non-secret) -> Cloud Run env vars ---
variable "drive_folder_id" {
  type        = string
  description = "Shared Drive folder id where output sheets are created."
}

variable "oauth_client_id" {
  type        = string
  description = "Google OAuth client id for the Squarespace sign-in (audience for /run ID tokens)."
}

variable "allowed_domain" {
  type        = string
  description = "Email domain allowed to call /run (e.g. abiliumtheagency.com)."
}

variable "allowed_emails" {
  type        = string
  default     = ""
  description = "Comma-separated final allow-list of /run callers."
}

variable "cors_origins" {
  type        = string
  default     = ""
  description = "Comma-separated browser origins allowed to call /run (the Squarespace site)."
}

variable "summary_email" {
  type        = string
  default     = ""
  description = "Recipient for the monthly run summary."
}

variable "inventory_dedupe" {
  type    = bool
  default = false
}

# Per-client Shopify app Client IDs (not secret). Keys must match clients.json.
variable "shopify_client_ids" {
  type = map(string)
  default = {
    enlightened_baby = ""
    kindred_spirits  = ""
    kick_pleat       = ""
  }
}

# --- Secret (value loaded out-of-band, never in tf/state) ---
variable "run_all_token" {
  type        = string
  sensitive   = true
  description = "Shared secret the Scheduler sends in X-Run-All-Token to invoke /run-all."
}

# --- Monthly summary email via Gmail SMTP (leave smtp_user empty to disable) ---
variable "smtp_host" {
  type    = string
  default = "" # set to smtp.gmail.com to enable
}

variable "smtp_port" {
  type    = string
  default = "587"
}

variable "smtp_user" {
  type        = string
  default     = ""
  description = "Sending Gmail/Workspace address. The App Password goes in the smtp-password secret."
}

variable "smtp_from" {
  type        = string
  default     = ""
  description = "From address (defaults to smtp_user)."
}
