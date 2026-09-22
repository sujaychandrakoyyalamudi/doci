output "artifact_repository" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.app.repository_id}"
}
output "application_url" {
  value = var.deploy_app ? google_cloud_run_v2_service.app[0].uri : local.app_url
}
output "task_target_url" {
  value = local.app_url
}
output "cloud_sql_connection" {
  value = google_sql_database_instance.main.connection_name
}
output "document_bucket" {
  value = google_storage_bucket.documents.name
}
output "build_service_account" {
  value = google_service_account.build.email
}
output "reviewer_url" {
  value = var.deploy_app && var.enable_a2a_reviewer ? google_cloud_run_v2_service.reviewer[0].uri : null
}
