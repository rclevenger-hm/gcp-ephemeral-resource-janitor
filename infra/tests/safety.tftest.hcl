mock_provider "google" {
  mock_data "google_project" {
    defaults = { number = "123456789012" }
  }
}
variables {
  project_id   = "janitor-test-123"
  state_bucket = "janitor-test-123-state"
  image        = "us-central1-docker.pkg.dev/janitor-test-123/images/runner@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
run "dryrun_permissions_and_invocation" {
  command = plan
  assert {
    condition = alltrue([for permission in google_project_iam_custom_role.runtime.permissions :
      !contains(["compute.instances.stop", "compute.instances.delete", "run.services.update",
    "container.clusters.update", "cloudscheduler.jobs.pause"], permission)])
    error_message = "Dry run must not receive workload mutation permissions."
  }
  assert {
    condition = (google_cloud_scheduler_job.timer.paused &&
      google_cloud_scheduler_job.timer.http_target[0].oauth_token[0].scope == "https://www.googleapis.com/auth/cloud-platform" &&
    google_cloud_run_v2_job_iam_member.invoke.role == "roles/run.invoker")
    error_message = "The timer must start paused and use the dedicated OAuth invoker grant."
  }
  assert {
    condition = (google_cloud_run_v2_job.janitor.template[0].task_count == 1 &&
      google_cloud_run_v2_job.janitor.template[0].parallelism == 1 &&
      google_cloud_run_v2_job.janitor.template[0].template[0].max_retries == 0 &&
    google_cloud_run_v2_job.janitor.template[0].template[0].timeout == "600s")
    error_message = "Task fan-out and automatic replay must remain disabled."
  }
  assert {
    condition = (google_storage_bucket.state.uniform_bucket_level_access &&
      google_storage_bucket.state.public_access_prevention == "enforced" &&
      !google_storage_bucket.state.force_destroy &&
    google_storage_bucket.state.versioning[0].enabled)
    error_message = "Durable audit storage must be private, versioned and protected from force destroy."
  }
  assert {
    condition     = length(google_service_account_iam_member.workload_identity) == 0
    error_message = "Dry run needs no workload service-account impersonation rights."
  }
}
run "live_compute_stop_only" {
  command = plan
  variables {
    dry_run  = false
    services = ["compute"]
  }
  assert {
    condition = (contains(google_project_iam_custom_role.runtime.permissions, "compute.instances.stop") &&
      !contains(google_project_iam_custom_role.runtime.permissions, "compute.instances.delete") &&
    !contains(google_project_iam_custom_role.runtime.permissions, "run.services.update"))
    error_message = "Stop mode must not grant deletion or unrelated service mutation."
  }
}
run "live_compute_lifecycle" {
  command = plan
  variables {
    dry_run  = false
    services = ["compute"]
    mode     = "lifecycle"
  }
  assert {
    condition     = contains(google_project_iam_custom_role.runtime.permissions, "compute.instances.delete")
    error_message = "Explicit live lifecycle mode requires the VM delete permission."
  }
}
run "cloud_run_requires_workload_identity" {
  command = plan
  variables {
    dry_run = false
  }
  expect_failures = [google_cloud_run_v2_job.janitor]
}
run "live_cloud_run_identity_is_exact" {
  command = plan
  variables {
    dry_run                    = false
    services                   = ["cloud_run"]
    cloud_run_service_accounts = ["app@janitor-test-123.iam.gserviceaccount.com"]
  }
  assert {
    condition = (length(google_service_account_iam_member.workload_identity) == 1 &&
      google_service_account_iam_member.workload_identity["app@janitor-test-123.iam.gserviceaccount.com"].service_account_id ==
    "projects/janitor-test-123/serviceAccounts/app@janitor-test-123.iam.gserviceaccount.com")
    error_message = "Service-account use must be granted on exact workload identities."
  }
}
