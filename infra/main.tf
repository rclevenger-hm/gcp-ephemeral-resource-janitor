data "google_project" "current" {
  project_id = var.project_id
}

locals {
  apis = toset([
    "compute.googleapis.com", "container.googleapis.com", "run.googleapis.com",
    "cloudscheduler.googleapis.com", "cloudfunctions.googleapis.com", "storage.googleapis.com",
    "cloudresourcemanager.googleapis.com", "iam.googleapis.com", "monitoring.googleapis.com",
    "logging.googleapis.com"
  ])
  reads = {
    compute          = ["compute.instances.get", "compute.instances.list", "compute.instances.listReferrers", "compute.zoneOperations.get"]
    cloud_run        = ["run.services.get", "run.services.list", "run.operations.get"]
    gke              = ["container.clusters.get", "container.clusters.list", "container.operations.get", "compute.instanceGroupManagers.get"]
    scheduler        = ["cloudscheduler.jobs.get"]
    legacy_functions = ["cloudfunctions.functions.get", "cloudfunctions.functions.list"]
  }
  writes = {
    compute          = concat(["compute.instances.stop"], var.mode == "lifecycle" ? ["compute.instances.delete"] : [])
    cloud_run        = ["run.services.update"]
    gke              = ["container.clusters.update"]
    scheduler        = ["cloudscheduler.jobs.pause"]
    legacy_functions = []
  }
  scheduler_name = "projects/${var.project_id}/locations/${var.region}/jobs/${var.name}"
  state_prefix   = "v1/projects/${data.google_project.current.number}/"
  policy = {
    project_id             = var.project_id
    project_number         = tostring(data.google_project.current.number)
    state_bucket           = var.state_bucket
    regions                = var.regions
    zones                  = var.zones
    services               = sort(tolist(var.services))
    dry_run                = var.dry_run
    mode                   = var.mode
    ttl_hours              = var.ttl_hours
    grace_hours            = var.grace_hours
    max_actions_per_run    = var.max_actions_per_run
    max_actions_per_window = var.max_actions_per_window
    window_seconds         = var.window_seconds
    max_resources          = 1000
    runtime_seconds        = 480
    scheduler_jobs = { for name, enrollment in var.scheduler_jobs : name => {
      for key, value in enrollment : key => value if value != null
    } }
    protected_resources = setunion(var.protected_resources, [local.scheduler_name])
  }
}

resource "google_project_service" "apis" {
  for_each           = local.apis
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_service_account" "runtime" {
  account_id   = var.name
  display_name = "Ephemeral resource janitor runtime"
  depends_on   = [google_project_service.apis]
}
resource "google_service_account" "scheduler" {
  account_id   = "${var.name}-timer"
  display_name = "Janitor timer invoker"
  depends_on   = [google_project_service.apis]
}

resource "google_project_iam_custom_role" "runtime" {
  role_id = "${replace(var.name, "-", "_")}_runtime"
  title   = "Ephemeral janitor bounded API operations"
  permissions = distinct(concat(
    ["resourcemanager.projects.get"],
    flatten([for service in var.services : local.reads[service]]),
    var.dry_run ? [] : flatten([for service in var.services : local.writes[service]])
  ))
}
resource "google_project_iam_member" "runtime" {
  project = var.project_id
  role    = google_project_iam_custom_role.runtime.name
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_service_account_iam_member" "workload_identity" {
  for_each           = !var.dry_run && contains(var.services, "cloud_run") ? var.cloud_run_service_accounts : toset([])
  service_account_id = "projects/${var.project_id}/serviceAccounts/${each.value}"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.runtime.email}"
}
resource "google_artifact_registry_repository_iam_member" "workload_images" {
  for_each   = !var.dry_run && contains(var.services, "cloud_run") ? var.cloud_run_image_repositories : toset([])
  project    = split("/", each.value)[1]
  location   = split("/", each.value)[3]
  repository = split("/", each.value)[5]
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_storage_bucket" "state" {
  name                        = var.state_bucket
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  versioning { enabled = true }
  # Only reports expire. Lock, first-seen history, intent and budgets never expire.
  lifecycle_rule {
    action { type = "Delete" }
    condition {
      age            = 90
      matches_prefix = ["${local.state_prefix}runs/"]
    }
  }
  lifecycle { prevent_destroy = true }
  depends_on = [google_project_service.apis]
}
resource "google_project_iam_custom_role" "state_objects" {
  role_id     = "${replace(var.name, "-", "_")}_state_objects"
  title       = "Janitor conditional state object access"
  permissions = ["storage.objects.get", "storage.objects.create", "storage.objects.delete"]
}
resource "google_project_iam_custom_role" "state_bucket" {
  role_id     = "${replace(var.name, "-", "_")}_state_bucket"
  title       = "Janitor bucket ownership verification"
  permissions = ["storage.buckets.get"]
}
resource "google_storage_bucket_iam_member" "state_objects" {
  bucket = google_storage_bucket.state.name
  role   = google_project_iam_custom_role.state_objects.name
  member = "serviceAccount:${google_service_account.runtime.email}"
  condition {
    title      = "janitor_project_state_only"
    expression = "resource.name.startsWith('projects/_/buckets/${var.state_bucket}/objects/${local.state_prefix}')"
  }
}
resource "google_storage_bucket_iam_member" "state_bucket" {
  bucket = google_storage_bucket.state.name
  role   = google_project_iam_custom_role.state_bucket.name
  member = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_cloud_run_v2_job" "janitor" {
  name                = var.name
  location            = var.region
  deletion_protection = true
  template {
    task_count  = 1
    parallelism = 1
    template {
      service_account = google_service_account.runtime.email
      max_retries     = 0
      timeout         = "600s"
      containers {
        image = var.image
        resources { limits = { cpu = "1", memory = "512Mi" } }
        env {
          name  = "JANITOR_POLICY"
          value = jsonencode(local.policy)
        }
      }
    }
  }
  lifecycle {
    precondition {
      condition = (contains(var.regions, var.region) &&
      alltrue([for z in var.zones : contains(var.regions, replace(z, "/-[a-z]$/", ""))]))
      error_message = "The job region and every zone must belong to configured regions."
    }
    precondition {
      condition     = var.dry_run || !contains(var.services, "cloud_run") || length(var.cloud_run_service_accounts) > 0
      error_message = "Live Cloud Run management requires an explicit list of workload service identities."
    }
    precondition {
      condition     = length(jsonencode(local.policy)) <= 32000
      error_message = "Policy exceeds the safe environment-variable size; split enrollment into a separate design."
    }
  }
  depends_on = [google_project_iam_member.runtime, google_storage_bucket_iam_member.state_objects,
    google_storage_bucket_iam_member.state_bucket, google_service_account_iam_member.workload_identity,
  google_artifact_registry_repository_iam_member.workload_images]
}
resource "google_cloud_run_v2_job_iam_member" "invoke" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.janitor.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}
resource "google_cloud_scheduler_job" "timer" {
  name             = var.name
  region           = var.region
  schedule         = var.schedule
  time_zone        = "Etc/UTC"
  paused           = var.scheduler_paused
  attempt_deadline = "60s"
  retry_config {
    retry_count          = 0
    max_retry_duration   = "0s"
    min_backoff_duration = "5s"
    max_backoff_duration = "5s"
    max_doublings        = 0
  }
  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${var.name}:run"
    body        = base64encode("{}")
    headers     = { "Content-Type" = "application/json" }
    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
  depends_on = [google_cloud_run_v2_job_iam_member.invoke]
}

resource "google_monitoring_alert_policy" "failure" {
  display_name          = "${var.name}: runtime or timer failure"
  combiner              = "OR"
  notification_channels = var.notification_channels
  conditions {
    display_name = "Janitor error or failed Scheduler dispatch"
    condition_matched_log {
      filter = <<-FILTER
        (resource.type="cloud_run_job" AND resource.labels.job_name="${var.name}" AND severity>=ERROR)
        OR (resource.type="cloud_scheduler_job" AND resource.labels.job_id="${var.name}" AND severity>=ERROR)
      FILTER
    }
  }
  alert_strategy {
    notification_rate_limit { period = "300s" }
    auto_close = "1800s"
  }
  documentation {
    content   = "Inspect the execution logs and durable state report. A retained lock or pending intent requires reconciliation; see docs/OPERATIONS.md. Scheduler success only confirms dispatch, not successful execution."
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.apis]
}
