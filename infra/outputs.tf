output "job_name" { value = google_cloud_run_v2_job.janitor.id }
output "scheduler_name" { value = google_cloud_scheduler_job.timer.id }
output "state_bucket" { value = google_storage_bucket.state.name }
output "state_prefix" { value = local.state_prefix }
output "runtime_identity" { value = google_service_account.runtime.email }
