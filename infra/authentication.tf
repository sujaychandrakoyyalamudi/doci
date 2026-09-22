locals {
  # This public web key identifies the authentication project. ID tokens and
  # trusted tenant/role claims are still required to read or change case data.
  firebase_api_key = var.firebase_api_key != "" ? var.firebase_api_key : nonsensitive(google_apikeys_key.authentication[0].key_string)
}

resource "google_apikeys_key" "authentication" {
  count        = var.firebase_api_key == "" ? 1 : 0
  name         = "${var.name}-web-auth"
  display_name = "Doci browser authentication only"
  restrictions {
    api_targets {
      service = "identitytoolkit.googleapis.com"
    }
    api_targets {
      service = "securetoken.googleapis.com"
    }
    browser_key_restrictions {
      allowed_referrers = concat(["${local.app_url}/*"], [for domain in local.custom_domains : "https://${domain}/*"])
    }
  }
  depends_on = [google_project_service.apis]
}
