data "google_project" "current" {
  project_id = var.project_id
}

locals {

  services = toset(["run.googleapis.com", "sqladmin.googleapis.com", "storage.googleapis.com",
    "aiplatform.googleapis.com", "documentai.googleapis.com", "cloudtasks.googleapis.com",
    "secretmanager.googleapis.com", "artifactregistry.googleapis.com", "cloudbuild.googleapis.com",
    "identitytoolkit.googleapis.com", "cloudtrace.googleapis.com", "monitoring.googleapis.com",
  "logging.googleapis.com", "iam.googleapis.com", "compute.googleapis.com", "apikeys.googleapis.com", "cloudresourcemanager.googleapis.com"])
  app_url      = "https://${var.name}-${data.google_project.current.number}.${var.region}.run.app"
  reviewer_url = "https://${var.name}-reviewer-${data.google_project.current.number}.${var.region}.run.app"
  secrets = {
    DATABASE_URL   = "postgresql+psycopg://doci:${random_password.database.result}@/doci?host=/cloudsql/${google_sql_database_instance.main.connection_name}"
    CHECKPOINT_URL = "postgresql://doci:${random_password.database.result}@/doci?host=/cloudsql/${google_sql_database_instance.main.connection_name}"
  }
  app_env = {

    APP_ENV                   = "production", MODEL_PROVIDER = "vertex", AUTH_MODE = "firebase",
    GOOGLE_CLOUD_PROJECT      = var.project_id, GOOGLE_CLOUD_LOCATION = var.region,
    GEMINI_MODEL              = var.gemini_model, GEMINI_LOCATION = var.gemini_location,
    FIREBASE_API_KEY          = local.firebase_api_key,
    FIREBASE_AUTH_DOMAIN      = var.firebase_auth_domain != "" ? var.firebase_auth_domain : "${var.project_id}.firebaseapp.com",
    STORAGE_BACKEND           = "gcs", GCS_BUCKET = google_storage_bucket.documents.name,
    QUEUE_BACKEND             = "cloud_tasks", CLOUD_TASKS_QUEUE = google_cloud_tasks_queue.reviews.name,
    WORKER_URL                = local.app_url, TASK_SERVICE_ACCOUNT = google_service_account.tasks.email,
    DOCUMENT_AI_PROCESSOR     = var.enable_document_ai ? google_document_ai_processor.ocr[0].id : "",
    DOCUMENT_AI_LOCATION      = var.document_ai_location, DATA_DIR = "/tmp/doci", CORS_ORIGINS = "[]",
    LANGSMITH_TRACING         = tostring(var.enable_langsmith)
    LANGSMITH_PROJECT         = var.langsmith_project != "" ? var.langsmith_project : "${var.name}-production"
    LANGSMITH_ENDPOINT        = var.langsmith_endpoint
    LANGSMITH_WORKSPACE_ID    = var.langsmith_workspace_id
    LANGSMITH_PROJECT_URL     = var.langsmith_project_url
    LANGSMITH_SAMPLING_RATE   = tostring(var.langsmith_sampling_rate)
    CLOUD_TRACE_SAMPLING_RATE = tostring(var.cloud_trace_sampling_rate)
    MONITORING_DASHBOARD_URL  = local.monitoring_enabled ? "https://console.cloud.google.com/monitoring/dashboards/builder/${basename(google_monitoring_dashboard.production[0].id)}?project=${var.project_id}" : ""
    LANGSMITH_HIDE_INPUTS     = "true", LANGSMITH_HIDE_OUTPUTS = "true",
    REVIEWER_A2A_URL          = var.enable_a2a_reviewer ? local.reviewer_url : ""
    SYNTHETIC_WORKSPACE       = tostring(var.synthetic_workspace)
    DATABASE_POOL_SIZE        = tostring(var.database_pool_size)
    DATABASE_MAX_OVERFLOW     = tostring(var.database_max_overflow)

  }

}

resource "google_project_service" "apis" {

  for_each           = local.services
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false

}

resource "google_artifact_registry_repository" "app" {

  location      = var.region
  repository_id = var.name
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]

}

resource "google_service_account" "app" {
  account_id   = "${var.name}-app"
  display_name = "Doci API and workflow runtime"
  depends_on   = [google_project_service.apis]
}
resource "google_service_account" "tasks" {
  account_id   = "${var.name}-tasks"
  display_name = "Doci authenticated task delivery"
  depends_on   = [google_project_service.apis]
}
resource "google_service_account" "reviewer" {
  account_id   = "${var.name}-reviewer"
  display_name = "Doci independent reviewer"
  depends_on   = [google_project_service.apis]
}
resource "google_service_account" "build" {
  account_id   = "${var.name}-build"
  display_name = "Doci CI build and deploy"
  depends_on   = [google_project_service.apis]
}

resource "google_project_iam_member" "app" {

  for_each = toset(["roles/aiplatform.user", "roles/cloudsql.client", "roles/documentai.apiUser",
  "roles/cloudtasks.enqueuer", "roles/cloudtrace.agent", "roles/logging.logWriter", "roles/firebaseauth.viewer"])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.app.email}"

}
resource "google_service_account_iam_member" "task_dispatch" {

  service_account_id = google_service_account.tasks.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.app.email}"

}
resource "google_project_iam_member" "reviewer" {

  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.reviewer.email}"

}
resource "google_project_iam_member" "build" {

  for_each = toset(["roles/artifactregistry.writer", "roles/run.developer", "roles/logging.logWriter", "roles/storage.objectViewer"])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.build.email}"

}
resource "google_service_account_iam_member" "build_runtime" {

  service_account_id = google_service_account.app.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.build.email}"

}

resource "google_storage_bucket" "documents" {

  name                        = "${var.project_id}-${var.name}-documents"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  versioning {
    enabled = true
  }
  depends_on = [google_project_service.apis]

}
resource "google_storage_bucket_iam_member" "documents" {

  bucket = google_storage_bucket.documents.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.app.email}"

}

resource "random_password" "database" {
  length  = 32
  special = false
}
resource "google_sql_database_instance" "main" {

  name                = "${var.name}-postgres"
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = var.deletion_protection
  settings {

    tier              = var.database_tier
    edition           = "ENTERPRISE"
    availability_type = var.database_ha ? "REGIONAL" : "ZONAL"
    disk_autoresize   = true
    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
    }
    # The Cloud SQL Auth Proxy mounted in Cloud Run authenticates and encrypts this connection.
    # There are no authorized public client networks.
    ip_configuration {
      ipv4_enabled = true
      ssl_mode     = "ENCRYPTED_ONLY"
    }
    insights_config {
      query_insights_enabled = !contains(["db-f1-micro", "db-g1-small"], var.database_tier)
    }

  }
  depends_on = [google_project_service.apis]

}
resource "google_sql_database" "app" {
  name     = "doci"
  instance = google_sql_database_instance.main.name
}
resource "google_sql_user" "app" {
  name     = "doci"
  instance = google_sql_database_instance.main.name
  password = random_password.database.result
}

resource "google_secret_manager_secret" "app" {

  for_each  = nonsensitive(toset(keys(local.secrets)))
  secret_id = "${var.name}-${lower(replace(each.key, "_", "-"))}"
  replication {
    auto {

    }
  }
  depends_on = [google_project_service.apis]

}
resource "google_secret_manager_secret_version" "app" {

  for_each    = nonsensitive(toset(keys(local.secrets)))
  secret      = google_secret_manager_secret.app[each.key].id
  secret_data = local.secrets[each.key]

}
resource "google_secret_manager_secret_iam_member" "app" {

  for_each  = google_secret_manager_secret.app
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app.email}"

}

resource "google_cloud_tasks_queue" "reviews" {

  name     = "${var.name}-review"
  location = var.region
  rate_limits {
    max_concurrent_dispatches = var.task_concurrency
    max_dispatches_per_second = 5
  }
  retry_config {
    max_attempts  = 10
    min_backoff   = "10s"
    max_backoff   = "300s"
    max_doublings = 4
  }
  stackdriver_logging_config {
    sampling_ratio = 1
  }
  depends_on = [google_project_service.apis]

}

resource "google_document_ai_processor" "ocr" {

  count        = var.enable_document_ai ? 1 : 0
  location     = var.document_ai_location
  display_name = "${var.name}-ocr"
  type         = "OCR_PROCESSOR"
  depends_on   = [google_project_service.apis]

}
resource "google_identity_platform_config" "auth" {

  count                      = var.manage_identity_platform ? 1 : 0
  project                    = var.project_id
  autodelete_anonymous_users = false
  authorized_domains         = concat(["localhost", "${var.project_id}.firebaseapp.com", "${var.project_id}.web.app"], local.custom_domains)
  sign_in {
    email {
      enabled           = true
      password_required = true
    }
    phone_number {
      enabled = false
    }
  }
  depends_on = [google_project_service.apis]

}

resource "google_cloud_run_v2_service" "app" {

  count               = var.deploy_app ? 1 : 0
  name                = var.name
  location            = var.region
  deletion_protection = var.deletion_protection
  ingress             = var.custom_domain != "" ? "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER" : "INGRESS_TRAFFIC_ALL"
  template {

    service_account                  = google_service_account.app.email
    timeout                          = "600s"
    max_instance_request_concurrency = var.max_request_concurrency
    scaling {
      min_instance_count = 0
      max_instance_count = var.max_instances
    }
    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.main.connection_name]
      }
    }
    containers {

      image = var.container_image
      ports {
        container_port = 8080
      }
      resources {
        limits = {
          cpu = "2", memory = "2Gi"
        }
        cpu_idle = true
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
      dynamic "env" {
        for_each = var.enable_langsmith ? [var.langsmith_api_key_secret_id] : []
        content {
          name = "LANGSMITH_API_KEY"
          value_source {
            secret_key_ref {
              secret  = env.value
              version = var.langsmith_secret_version
            }
          }
        }
      }
      startup_probe {
        http_get {
          path = "/readyz"
        }
        initial_delay_seconds = 10
        period_seconds        = 10
        failure_threshold     = 18
      }
      liveness_probe {
        http_get {
          path = "/healthz"
        }
        period_seconds = 30
      }

    }

  }
  lifecycle {

    precondition {
      condition     = var.container_image != ""
      error_message = "Provide a built application image before deployment."
    }

  }
  depends_on = [google_secret_manager_secret_iam_member.app, google_secret_manager_secret_iam_member.langsmith, google_project_iam_member.app,
  google_sql_database.app, google_sql_user.app, google_secret_manager_secret_version.app]

}
resource "google_cloud_run_v2_service_iam_member" "task_invoker" {

  count    = var.deploy_app ? 1 : 0
  name     = google_cloud_run_v2_service.app[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.tasks.email}"

}
resource "google_cloud_run_v2_service_iam_member" "web_access" {

  count    = var.deploy_app && var.allow_public_app ? 1 : 0
  name     = google_cloud_run_v2_service.app[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"

}

resource "google_cloud_run_v2_service" "reviewer" {

  count               = var.deploy_app && var.enable_a2a_reviewer ? 1 : 0
  name                = "${var.name}-reviewer"
  location            = var.region
  deletion_protection = var.deletion_protection
  template {

    service_account = google_service_account.reviewer.email
    timeout         = "300s"
    scaling {
      max_instance_count = var.max_instances
    }
    containers {

      image   = var.container_image
      command = ["uvicorn"]
      args    = ["doci.integrations.a2a_server:app", "--host", "0.0.0.0", "--port", "8080"]
      resources {
        limits = {
          cpu = "1", memory = "1Gi"
        }
      }
      dynamic "env" {

        for_each = {
          MODEL_PROVIDER        = "vertex", GOOGLE_CLOUD_PROJECT = var.project_id,
          GOOGLE_CLOUD_LOCATION = var.region, GEMINI_MODEL = var.gemini_model,
          GEMINI_LOCATION       = var.gemini_location, A2A_PUBLIC_URL = local.reviewer_url
        }
        content {
          name  = env.key
          value = env.value
        }

      }

    }

  }
  depends_on = [google_project_iam_member.reviewer]

}
resource "google_cloud_run_v2_service_iam_member" "reviewer_invoker" {

  count    = var.deploy_app && var.enable_a2a_reviewer ? 1 : 0
  name     = google_cloud_run_v2_service.reviewer[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.app.email}"

}

resource "google_cloud_run_v2_job" "reindex" {

  count               = var.deploy_app ? 1 : 0
  name                = "${var.name}-reindex"
  location            = var.region
  deletion_protection = var.deletion_protection
  template {
    template {

      service_account = google_service_account.app.email
      max_retries     = 1
      timeout         = "3600s"
      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [google_sql_database_instance.main.connection_name]
        }
      }
      containers {

        image   = var.container_image
        command = ["python"]
        args    = ["-m", "doci.jobs", "reindex"]
        resources {
          limits = {
            cpu = "1", memory = "1Gi"
          }
        }
        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }
        dynamic "env" {
          for_each = merge(local.app_env, { LANGSMITH_TRACING = "false" })
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
