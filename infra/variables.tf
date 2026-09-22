variable "project_id" {
  type = string
}
variable "region" {
  type    = string
  default = "us-central1"
}
variable "name" {
  type    = string
  default = "doci"
}
variable "container_image" {
  type    = string
  default = ""
}
variable "deploy_app" {
  type    = bool
  default = false
}
variable "allow_public_app" {
  type    = bool
  default = false
}
variable "database_tier" {
  type    = string
  default = "db-custom-1-3840"
}
variable "database_ha" {
  type    = bool
  default = true
}
variable "deletion_protection" {
  type    = bool
  default = true
}
variable "firebase_api_key" {
  type    = string
  default = ""
}
variable "firebase_auth_domain" {
  type    = string
  default = ""
}
variable "manage_identity_platform" {
  type    = bool
  default = true
}
variable "enable_document_ai" {
  type    = bool
  default = true
}
variable "document_ai_location" {
  type    = string
  default = "us"
}
variable "gemini_model" {
  type    = string
  default = "gemini-3.5-flash"
}
variable "gemini_location" {
  type    = string
  default = "global"
}
variable "enable_langsmith" {
  type    = bool
  default = false
}
variable "enable_a2a_reviewer" {
  type    = bool
  default = false
}
variable "max_instances" {
  type    = number
  default = 5
}
variable "custom_domain" {
  description = "Apex domain managed at your registrar; empty disables the HTTPS load balancer."
  type        = string
  default     = ""
  validation {
    condition     = var.custom_domain == "" || can(regex("^[a-z0-9][a-z0-9.-]+\\.[a-z]{2,}$", var.custom_domain))
    error_message = "Use a lowercase hostname without a URL scheme, path, or wildcard."
  }
}
variable "include_www" {
  type    = bool
  default = true
}
variable "tls_certificate_revision" {
  description = "Increment to request a replacement managed certificate after correcting validation failures."
  type        = number
  default     = 1
  validation {
    condition     = var.tls_certificate_revision >= 1 && floor(var.tls_certificate_revision) == var.tls_certificate_revision
    error_message = "Use a positive integer certificate revision."
  }
}
variable "synthetic_workspace" {
  description = "Marks the workspace as containing synthetic test records; authentication remains enabled."
  type        = bool
  default     = false
}
variable "test_tenant" {
  type    = string
  default = "test-workspace"
  validation {
    condition     = startswith(var.test_tenant, "test-")
    error_message = "The synthetic dataset must use an isolated test- tenant."
  }
}
variable "seed_dataset_uri" {
  description = "Private GCS dataset URI for an explicitly executed evaluation import; no records are bundled."
  type        = string
  default     = ""
}
variable "database_pool_size" {
  type    = number
  default = 5
}
variable "database_max_overflow" {
  type    = number
  default = 10
}
variable "max_request_concurrency" {
  type    = number
  default = 16
}
variable "task_concurrency" {
  type    = number
  default = 10
}
