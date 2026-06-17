# One-time bootstrap: creates the GCS bucket that holds remote Terraform state
# for the main stack. Run locally with default (local) state, then commit and
# point infra/backend.tf at the bucket this outputs.
#
#   cd infra/bootstrap
#   terraform init
#   terraform apply -var project=<PROJECT> -var state_bucket=<GLOBALLY-UNIQUE-NAME>

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project
  region  = var.region
}

variable "project" {
  type        = string
  description = "GCP project id."
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "state_bucket" {
  type        = string
  description = "Globally-unique GCS bucket name for Terraform state."
}

resource "google_storage_bucket" "tfstate" {
  name                        = var.state_bucket
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false

  versioning {
    enabled = true
  }
}

output "state_bucket" {
  value = google_storage_bucket.tfstate.name
}
