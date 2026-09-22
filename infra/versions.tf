terraform {

  required_version = ">= 1.7"
  required_providers {

    google = {
      source = "hashicorp/google", version = "~> 7.0"
    }
    random = {
      source = "hashicorp/random", version = "~> 3.7"
    }

  }
  # Configure the bucket/prefix through terraform init -backend-config.
  backend "gcs" {}

}

provider "google" {

  project               = var.project_id
  region                = var.region
  billing_project       = var.project_id
  user_project_override = true

}
