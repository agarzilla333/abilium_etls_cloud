# Remote state in the bucket created by infra/bootstrap. Fill in the bucket name
# (bootstrap's `state_bucket` output), then `terraform init`.
terraform {
  backend "gcs" {
    bucket = "abilium-etls-tfstate"
    prefix = "abilium-etl"
  }
}
