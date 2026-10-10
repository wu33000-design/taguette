terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

variable "project_id" {
  type = string
}
variable "region" {
  type    = string
  default = "asia-east1"
}
variable "sql_instance" {
  type    = string
  default = "taguette-db"
}
variable "database_tier" {
  type    = string
  default = "db-f1-micro"
}

provider "google" {
  project = var.project_id
  region  = var.region
}

resource "google_project_service" "api" {
  for_each = toset([
    "run.googleapis.com", "sqladmin.googleapis.com",
    "secretmanager.googleapis.com", "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com", "cloudscheduler.googleapis.com",
    "iam.googleapis.com", "storage.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

resource "google_sql_database_instance" "taguette" {
  name                = var.sql_instance
  region              = var.region
  database_version    = "POSTGRES_16"
  deletion_protection = true
  depends_on          = [google_project_service.api]
  settings {
    edition                     = "ENTERPRISE"
    tier                        = var.database_tier
    availability_type           = "ZONAL"
    disk_type                   = "PD_SSD"
    disk_size                   = 10
    disk_autoresize             = true
    disk_autoresize_limit       = 50
    deletion_protection_enabled = true
    ip_configuration {
      ipv4_enabled = true
      # No authorized_networks: connections use Cloud SQL Auth Proxy.
    }
    backup_configuration {
      enabled                        = true
      start_time                     = "19:00"
      point_in_time_recovery_enabled = true
      transaction_log_retention_days = 7
      backup_retention_settings {
        retained_backups = 14
        retention_unit   = "COUNT"
      }
    }
    maintenance_window {
      day          = 7
      hour         = 18
      update_track = "stable"
    }
  }
  lifecycle {
    ignore_changes = [settings[0].disk_size]
  }
}

resource "google_sql_database" "taguette" {
  name     = "taguette"
  instance = google_sql_database_instance.taguette.name
}

resource "google_service_account" "account" {
  for_each     = toset(["web", "manage", "backup", "scheduler"])
  account_id   = "taguette-${each.key}"
  display_name = "Taguette ${each.key}"
  depends_on   = [google_project_service.api]
}

resource "google_project_iam_member" "sql_client" {
  for_each = toset(["web", "manage", "backup"])
  project  = var.project_id
  role     = "roles/cloudsql.client"
  member   = "serviceAccount:${google_service_account.account[each.key].email}"
}

# Empty secret containers only. Populate secret versions outside Terraform.
# Database users/passwords are also provisioned outside Terraform state.
resource "google_secret_manager_secret" "secret" {
  for_each = toset(["app-password", "owner-password", "backup-password",
  "admin-password", "cookie-secret"])
  secret_id = "taguette-${each.key}"
  replication {
    auto {}
  }
  depends_on = [google_project_service.api]
}

locals {
  secret_access = {
    "web-app"       = { account = "web", secret = "app-password" }
    "web-cookie"    = { account = "web", secret = "cookie-secret" }
    "manage-owner"  = { account = "manage", secret = "owner-password" }
    "manage-admin"  = { account = "manage", secret = "admin-password" }
    "backup-reader" = { account = "backup", secret = "backup-password" }
  }
}

resource "google_secret_manager_secret_iam_member" "access" {
  for_each  = local.secret_access
  secret_id = google_secret_manager_secret.secret[each.value.secret].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.account[each.value.account].email}"
}

resource "google_storage_bucket" "bucket" {
  for_each                    = toset(["backups", "originals"])
  name                        = "${var.project_id}-taguette-${each.key}"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  depends_on                  = [google_project_service.api]
  dynamic "lifecycle_rule" {
    for_each = each.key == "backups" ? [1] : []
    content {
      action {
        type = "Delete"
      }
      condition {
        age = 56
      }
    }
  }
}

resource "google_storage_bucket_iam_member" "backup_writer" {
  bucket = google_storage_bucket.bucket["backups"].name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.account["backup"].email}"
}

resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = "taguette"
  format        = "DOCKER"
  depends_on    = [google_project_service.api]
}

output "instance_connection_name" {
  value = google_sql_database_instance.taguette.connection_name
}
output "backup_bucket" {
  value = google_storage_bucket.bucket["backups"].name
}
