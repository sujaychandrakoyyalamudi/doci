variable "langsmith_api_key_secret_id" {
  description = "Existing Secret Manager secret ID in this project. Secret values are managed outside Terraform."
  type        = string
  default     = ""
  validation {
    condition     = var.langsmith_api_key_secret_id == "" || can(regex("^[a-zA-Z0-9_-]+$", var.langsmith_api_key_secret_id))
    error_message = "Provide a Secret Manager secret ID, not a key value or URL."
  }
}
variable "langsmith_secret_version" {
  type    = string
  default = "latest"
}
variable "langsmith_project" {
  type    = string
  default = ""
}
variable "langsmith_endpoint" {
  type    = string
  default = "https://api.smith.langchain.com"
}
variable "langsmith_workspace_id" {
  type    = string
  default = ""
}
variable "langsmith_project_url" {
  type    = string
  default = ""
}
variable "langsmith_sampling_rate" {
  type    = number
  default = 1.0
  validation {
    condition     = var.langsmith_sampling_rate >= 0 && var.langsmith_sampling_rate <= 1
    error_message = "Set the review sampling rate between zero and one."
  }
}
variable "cloud_trace_sampling_rate" {
  type    = number
  default = 0.1
  validation {
    condition     = var.cloud_trace_sampling_rate >= 0 && var.cloud_trace_sampling_rate <= 1
    error_message = "Set the HTTP trace sampling rate between zero and one."
  }
}
variable "enable_monitoring" {
  type    = bool
  default = true
}
variable "monitoring_notification_channels" {
  description = "Existing notification channel IDs; empty keeps alerts in the console only."
  type        = list(string)
  default     = []
}
variable "review_latency_threshold_ms" {
  type    = number
  default = 180000
}
variable "queue_backlog_threshold" {
  type    = number
  default = 20
}

resource "google_secret_manager_secret_iam_member" "langsmith" {
  count      = var.enable_langsmith ? 1 : 0
  secret_id  = var.langsmith_api_key_secret_id
  role       = "roles/secretmanager.secretAccessor"
  member     = "serviceAccount:${google_service_account.app.email}"
  depends_on = [google_project_service.apis]
  lifecycle {
    precondition {
      condition     = var.langsmith_api_key_secret_id != ""
      error_message = "Create the LangSmith secret outside Terraform and provide its ID before enabling tracing."
    }
  }
}

locals {
  monitoring_enabled  = var.deploy_app && var.enable_monitoring
  runtime_log_filter  = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${var.name}\""
  run_metric_filter   = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${var.name}\""
  queue_metric_filter = "resource.type=\"cloud_tasks_queue\" AND resource.labels.queue_id=\"${var.name}-review\""
  sql_metric_filter   = "resource.type=\"cloudsql_database\" AND resource.labels.database_id=\"${var.project_id}:${google_sql_database_instance.main.name}\""
  log_counters = {
    review_executions       = { event = "workflow.execution_finished", description = "Graph execution and resumption count; not distinct case count." }
    workflow_failures       = { event = "workflow.failed", description = "Workflow execution errors requiring recovery." }
    telemetry_failures      = { event = "telemetry.delivery_failed", description = "Trace export or feedback delivery errors." }
    queue_dispatch_failures = { event = "queue.dispatch_failed", description = "Reviews saved but not successfully queued." }
    human_decisions         = { event = "review.human_decision", description = "Persisted human decision submissions." }
  }
}

resource "google_logging_metric" "events" {
  for_each    = local.monitoring_enabled ? local.log_counters : {}
  name        = "${var.name}/${each.key}"
  description = each.value.description
  filter      = "${local.runtime_log_filter} AND jsonPayload.event=\"${each.value.event}\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
  depends_on = [google_project_service.apis]
}

resource "google_logging_metric" "review_latency" {
  count           = local.monitoring_enabled ? 1 : 0
  name            = "${var.name}/review_execution_duration_ms"
  description     = "Time spent executing a review graph; human approval waiting time is excluded."
  filter          = "${local.runtime_log_filter} AND jsonPayload.event=\"workflow.execution_finished\""
  value_extractor = "EXTRACT(jsonPayload.duration_ms)"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "ms"
  }
  bucket_options {
    exponential_buckets {
      num_finite_buckets = 20
      growth_factor      = 2
      scale              = 1
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_monitoring_uptime_check_config" "application" {
  count            = local.monitoring_enabled && var.custom_domain != "" ? 1 : 0
  display_name     = "${var.name} HTTPS and database readiness"
  timeout          = "10s"
  period           = "60s"
  selected_regions = ["USA", "EUROPE", "ASIA_PACIFIC"]
  http_check {
    path         = "/readyz"
    port         = 443
    use_ssl      = true
    validate_ssl = true
    accepted_response_status_codes { status_class = "STATUS_CLASS_2XX" }
  }
  monitored_resource {
    type   = "uptime_url"
    labels = { project_id = var.project_id, host = var.custom_domain }
  }
  content_matchers { content = "\"status\":\"ready\"" }
  depends_on = [google_project_service.apis]
}

resource "google_monitoring_alert_policy" "api_errors" {
  count                 = local.monitoring_enabled ? 1 : 0
  display_name          = "${var.name} elevated server errors"
  combiner              = "AND"
  notification_channels = var.monitoring_notification_channels
  conditions {
    display_name = "5xx exceeds 5 percent for five minutes"
    condition_threshold {
      filter             = "${local.run_metric_filter} AND metric.type=\"run.googleapis.com/request_count\" AND metric.labels.response_code_class=\"5xx\""
      denominator_filter = "${local.run_metric_filter} AND metric.type=\"run.googleapis.com/request_count\""
      comparison         = "COMPARISON_GT"
      threshold_value    = 0.05
      duration           = "300s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.labels.service_name"]
      }
      denominator_aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.labels.service_name"]
      }
    }
  }
  conditions {
    display_name = "At least five server errors in the window"
    condition_threshold {
      filter          = "${local.run_metric_filter} AND metric.type=\"run.googleapis.com/request_count\" AND metric.labels.response_code_class=\"5xx\""
      comparison      = "COMPARISON_GT"
      threshold_value = 4
      duration        = "0s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.labels.service_name"]
      }
    }
  }
  documentation {
    content   = "Inspect API and worker logs, the current revision, database readiness, and the production dashboard. The ratio condition requires a minimum error volume to reduce low-traffic noise. See docs/OBSERVABILITY.md."
    mime_type = "text/markdown"
  }
  alert_strategy { auto_close = "1800s" }
  depends_on = [google_project_service.apis]
}

resource "google_monitoring_alert_policy" "event_errors" {
  for_each              = local.monitoring_enabled ? { for key, value in local.log_counters : key => value if contains(["workflow_failures", "telemetry_failures", "queue_dispatch_failures"], key) } : {}
  display_name          = "${var.name} ${replace(each.key, "_", " ")}"
  combiner              = "OR"
  notification_channels = var.monitoring_notification_channels
  conditions {
    display_name = each.value.description
    condition_threshold {
      filter          = "${local.run_metric_filter} AND metric.type=\"logging.googleapis.com/user/${google_logging_metric.events[each.key].name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }
  documentation {
    content   = "Open the production dashboard and filter structured logs by event and review_id. Telemetry outages must not block case processing. Retry failed reviews after addressing the underlying dependency. See docs/OBSERVABILITY.md."
    mime_type = "text/markdown"
  }
  alert_strategy { auto_close = "1800s" }
}

resource "google_monitoring_alert_policy" "review_latency" {
  count                 = local.monitoring_enabled ? 1 : 0
  display_name          = "${var.name} slow review execution"
  combiner              = "OR"
  notification_channels = var.monitoring_notification_channels
  conditions {
    display_name = "Review p95 execution duration is elevated"
    condition_threshold {
      filter          = "${local.run_metric_filter} AND metric.type=\"logging.googleapis.com/user/${google_logging_metric.review_latency[0].name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.review_latency_threshold_ms
      duration        = "300s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_PERCENTILE_95"
        cross_series_reducer = "REDUCE_MAX"
      }
    }
  }
  documentation {
    content   = "Inspect LangSmith model and graph timings. Check model availability, database latency, and retry counts. This metric excludes time waiting for human approval; it uses the highest revision-level p95. See docs/OBSERVABILITY.md."
    mime_type = "text/markdown"
  }
  alert_strategy { auto_close = "1800s" }
}

resource "google_monitoring_alert_policy" "queue_backlog" {
  count                 = local.monitoring_enabled ? 1 : 0
  display_name          = "${var.name} sustained review queue backlog"
  combiner              = "OR"
  notification_channels = var.monitoring_notification_channels
  conditions {
    display_name = "Pending task count stays above the configured threshold"
    condition_threshold {
      filter          = "${local.queue_metric_filter} AND metric.type=\"cloudtasks.googleapis.com/queue/depth\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.queue_backlog_threshold
      duration        = "600s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_MAX"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }
  documentation {
    content   = "Check Cloud Tasks delivery attempts, worker authorization, Cloud Run capacity, and workflow failures. Review the configured concurrency before increasing it. See docs/OBSERVABILITY.md."
    mime_type = "text/markdown"
  }
  alert_strategy { auto_close = "1800s" }
  depends_on = [google_project_service.apis]
}

resource "google_monitoring_alert_policy" "availability" {
  count                 = local.monitoring_enabled && var.custom_domain != "" ? 1 : 0
  display_name          = "${var.name} HTTPS readiness unavailable"
  combiner              = "OR"
  notification_channels = var.monitoring_notification_channels
  conditions {
    display_name = "Two or more uptime locations fail readiness"
    condition_threshold {
      filter          = "resource.type=\"uptime_url\" AND metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND metric.labels.check_id=\"${google_monitoring_uptime_check_config.application[0].uptime_check_id}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "180s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
      }
    }
  }
  documentation {
    content   = "Check the domain's TLS certificate, load balancer, Cloud Run revision, and Cloud SQL. The /readyz endpoint validates a database connection. At least two probing locations must fail for three minutes. See docs/OBSERVABILITY.md."
    mime_type = "text/markdown"
  }
  alert_strategy { auto_close = "1800s" }
}

locals {
  dashboard_charts = local.monitoring_enabled ? [
    { title = "HTTP request rate", filter = "${local.run_metric_filter} AND metric.type=\"run.googleapis.com/request_count\"", aligner = "ALIGN_RATE", reducer = "REDUCE_SUM", unit = "requests/s" },
    { title = "HTTP server error rate", filter = "${local.run_metric_filter} AND metric.type=\"run.googleapis.com/request_count\" AND metric.labels.response_code_class=\"5xx\"", aligner = "ALIGN_RATE", reducer = "REDUCE_SUM", unit = "errors/s" },
    { title = "HTTP latency p95 (includes workers)", filter = "${local.run_metric_filter} AND metric.type=\"run.googleapis.com/request_latencies\"", aligner = "ALIGN_PERCENTILE_95", reducer = "REDUCE_MAX", unit = "ms" },
    { title = "Review execution duration p95", filter = "${local.run_metric_filter} AND metric.type=\"logging.googleapis.com/user/${google_logging_metric.review_latency[0].name}\"", aligner = "ALIGN_PERCENTILE_95", reducer = "REDUCE_MAX", unit = "ms" },
    { title = "Review executions and resumptions", filter = "${local.run_metric_filter} AND metric.type=\"logging.googleapis.com/user/${google_logging_metric.events["review_executions"].name}\"", aligner = "ALIGN_SUM", reducer = "REDUCE_SUM", unit = "executions" },
    { title = "Workflow failures", filter = "${local.run_metric_filter} AND metric.type=\"logging.googleapis.com/user/${google_logging_metric.events["workflow_failures"].name}\"", aligner = "ALIGN_SUM", reducer = "REDUCE_SUM", unit = "failures" },
    { title = "Telemetry delivery failures", filter = "${local.run_metric_filter} AND metric.type=\"logging.googleapis.com/user/${google_logging_metric.events["telemetry_failures"].name}\"", aligner = "ALIGN_SUM", reducer = "REDUCE_SUM", unit = "errors" },
    { title = "Persisted human decision submissions", filter = "${local.run_metric_filter} AND metric.type=\"logging.googleapis.com/user/${google_logging_metric.events["human_decisions"].name}\"", aligner = "ALIGN_SUM", reducer = "REDUCE_SUM", unit = "submissions" },
    { title = "Pending Cloud Tasks", filter = "${local.queue_metric_filter} AND metric.type=\"cloudtasks.googleapis.com/queue/depth\"", aligner = "ALIGN_MAX", reducer = "REDUCE_SUM", unit = "tasks" },
    { title = "Task dispatch delay p95", filter = "${local.queue_metric_filter} AND metric.type=\"cloudtasks.googleapis.com/queue/task_attempt_delays\"", aligner = "ALIGN_PERCENTILE_95", reducer = "REDUCE_MAX", unit = "ms" },
    { title = "Cloud Run instances", filter = "${local.run_metric_filter} AND metric.type=\"run.googleapis.com/container/instance_count\"", aligner = "ALIGN_MAX", reducer = "REDUCE_SUM", unit = "instances" },
    { title = "Cloud SQL CPU utilization", filter = "${local.sql_metric_filter} AND metric.type=\"cloudsql.googleapis.com/database/cpu/utilization\"", aligner = "ALIGN_MEAN", reducer = "REDUCE_MAX", unit = "ratio" },
    { title = "Cloud SQL memory utilization", filter = "${local.sql_metric_filter} AND metric.type=\"cloudsql.googleapis.com/database/memory/utilization\"", aligner = "ALIGN_MEAN", reducer = "REDUCE_MAX", unit = "ratio" },
    { title = "Cloud SQL connections", filter = "${local.sql_metric_filter} AND metric.type=\"cloudsql.googleapis.com/database/postgresql/num_backends\"", aligner = "ALIGN_MAX", reducer = "REDUCE_SUM", unit = "connections" },
  ] : []
}
resource "google_monitoring_dashboard" "production" {
  count = local.monitoring_enabled ? 1 : 0
  dashboard_json = jsonencode({
    displayName = "${var.name} — Production monitoring"
    mosaicLayout = {
      columns = 12
      tiles = [for index, chart in local.dashboard_charts : merge({
        width  = 6
        height = 4
        widget = {
          title = chart.title
          xyChart = {
            dataSets = [{
              timeSeriesQuery = { timeSeriesFilter = {
                filter      = chart.filter
                aggregation = { alignmentPeriod = "60s", perSeriesAligner = chart.aligner, crossSeriesReducer = chart.reducer }
              } }
              plotType   = "LINE"
              targetAxis = "Y1"
            }]
            yAxis = { label = chart.unit, scale = "LINEAR" }
          }
        }
      }, index % 2 == 1 ? { xPos = 6 } : {}, index >= 2 ? { yPos = floor(index / 2) * 4 } : {})]
    }
  })
  depends_on = [google_project_service.apis]
}

output "monitoring_dashboard_url" {
  value = local.monitoring_enabled ? "https://console.cloud.google.com/monitoring/dashboards/builder/${basename(google_monitoring_dashboard.production[0].id)}?project=${var.project_id}" : null
}
output "langsmith_project_url" {
  value = var.enable_langsmith ? var.langsmith_project_url : null
}
