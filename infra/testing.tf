resource "google_cloud_run_v2_job" "seed_testing" {
  count               = var.deploy_app && var.synthetic_workspace ? 1 : 0
  name                = "${var.name}-seed-testing"
  location            = var.region
  deletion_protection = var.deletion_protection
  template {
    template {
      service_account = google_service_account.app.email
      max_retries     = 1
      timeout         = "1800s"
      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [google_sql_database_instance.main.connection_name]
        }
      }
      containers {
        image   = var.container_image
        command = ["python"]
        args    = ["-m", "doci.seed_testing", "--tenant", var.test_tenant]
        env {
          name  = "SEED_DATASET_URI"
          value = var.seed_dataset_uri
        }
        resources {
          limits = { cpu = "1", memory = "1Gi" }
        }
        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }
        dynamic "env" {
          for_each = local.app_env
          content {
            name  = env.key
            value = env.value
          }
        }
        dynamic "env" {
          for_each = google_secret_manager_secret.app
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = env.value.secret_id
                version = google_secret_manager_secret_version.app[env.key].version
              }
            }
          }
        }
      }
    }
  }
  depends_on = [google_cloud_run_v2_service.app]
}

output "seed_testing_job" {
  value = var.deploy_app && var.synthetic_workspace ? google_cloud_run_v2_job.seed_testing[0].name : null
}
