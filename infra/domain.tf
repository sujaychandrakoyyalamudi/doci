locals {
  domain_enabled = var.deploy_app && var.custom_domain != ""
  custom_domains = var.custom_domain == "" ? [] : concat(
    [var.custom_domain], var.include_www ? ["www.${var.custom_domain}"] : []
  )
}

resource "google_compute_global_address" "web" {
  count      = local.domain_enabled ? 1 : 0
  name       = "${var.name}-web-ip"
  ip_version = "IPV4"
  depends_on = [google_project_service.apis]
}

resource "google_compute_region_network_endpoint_group" "web" {
  count                 = local.domain_enabled ? 1 : 0
  name                  = "${var.name}-serverless"
  region                = var.region
  network_endpoint_type = "SERVERLESS"
  cloud_run {
    service = google_cloud_run_v2_service.app[0].name
  }
}

resource "google_compute_backend_service" "web" {
  count                 = local.domain_enabled ? 1 : 0
  name                  = "${var.name}-web-backend"
  protocol              = "HTTP"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  enable_cdn            = false
  backend {
    group = google_compute_region_network_endpoint_group.web[0].id
  }
}

resource "google_compute_url_map" "web" {
  count           = local.domain_enabled ? 1 : 0
  name            = "${var.name}-web"
  default_service = google_compute_backend_service.web[0].id
}

resource "google_compute_managed_ssl_certificate" "web" {
  count = local.domain_enabled ? 1 : 0
  name  = "${var.name}-tls-${substr(sha256(join(",", local.custom_domains)), 0, 8)}-v${var.tls_certificate_revision}"
  managed {
    domains = local.custom_domains
  }
  lifecycle {
    create_before_destroy = true
  }
}

resource "google_compute_ssl_policy" "web" {
  count           = local.domain_enabled ? 1 : 0
  name            = "${var.name}-tls-policy"
  profile         = "MODERN"
  min_tls_version = "TLS_1_2"
}

resource "google_compute_target_https_proxy" "web" {
  count            = local.domain_enabled ? 1 : 0
  name             = "${var.name}-https"
  url_map          = google_compute_url_map.web[0].id
  ssl_certificates = [google_compute_managed_ssl_certificate.web[0].id]
  ssl_policy       = google_compute_ssl_policy.web[0].id
}

resource "google_compute_global_forwarding_rule" "https" {
  count                 = local.domain_enabled ? 1 : 0
  name                  = "${var.name}-https"
  ip_address            = google_compute_global_address.web[0].address
  port_range            = "443"
  target                = google_compute_target_https_proxy.web[0].id
  load_balancing_scheme = "EXTERNAL_MANAGED"
}

resource "google_compute_url_map" "http_redirect" {
  count = local.domain_enabled ? 1 : 0
  name  = "${var.name}-http-redirect"
  default_url_redirect {
    https_redirect         = true
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
    strip_query            = false
  }
}

resource "google_compute_target_http_proxy" "redirect" {
  count   = local.domain_enabled ? 1 : 0
  name    = "${var.name}-http-redirect"
  url_map = google_compute_url_map.http_redirect[0].id
}

resource "google_compute_global_forwarding_rule" "http" {
  count                 = local.domain_enabled ? 1 : 0
  name                  = "${var.name}-http"
  ip_address            = google_compute_global_address.web[0].address
  port_range            = "80"
  target                = google_compute_target_http_proxy.redirect[0].id
  load_balancing_scheme = "EXTERNAL_MANAGED"
}

output "custom_domain_url" {
  value = local.domain_enabled ? "https://${var.custom_domain}" : null
}

output "squarespace_dns_records" {
  value = local.domain_enabled ? concat([
    { host = "@", type = "A", value = google_compute_global_address.web[0].address, ttl = 1800 }
    ], var.include_www ? [
    { host = "www", type = "A", value = google_compute_global_address.web[0].address, ttl = 1800 }
  ] : []) : []
}

output "managed_certificate_name" {
  value = local.domain_enabled ? google_compute_managed_ssl_certificate.web[0].name : null
}
