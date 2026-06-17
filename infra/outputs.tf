output "service_url" {
  value       = google_cloud_run_v2_service.app.uri
  description = "Cloud Run URL. Register <url>/oauth/callback in each Shopify app's allowed redirects."
}

output "runtime_service_account" {
  value       = google_service_account.runtime.email
  description = "Share the Shared Drive with this account (Content Manager) so it can write sheets."
}

output "token_secret_ids" {
  value = values(local.token_secrets)
}

output "oauth_client_secret_ids" {
  value = values(local.oauth_secrets)
}
