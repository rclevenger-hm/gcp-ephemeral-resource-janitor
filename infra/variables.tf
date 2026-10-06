variable "project_id" {
  type        = string
  description = "One explicitly selected project; no organization-wide discovery."
}
variable "region" {
  type    = string
  default = "us-central1"
}
variable "regions" {
  type    = list(string)
  default = ["us-central1"]
}
variable "zones" {
  type    = list(string)
  default = ["us-central1-a"]
}
variable "name" {
  type    = string
  default = "ephemeral-janitor"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{3,22}[a-z0-9]$", var.name))
    error_message = "Use a lowercase resource name between 5 and 24 characters."
  }
}
variable "image" {
  type        = string
  description = "Prebuilt Artifact Registry image pinned by sha256 digest."
  validation {
    condition     = can(regex("^[a-z0-9-]+-docker\\.pkg\\.dev/.+@sha256:[a-f0-9]{64}$", var.image))
    error_message = "Supply an Artifact Registry image with an immutable sha256 digest."
  }
}
variable "state_bucket" {
  type        = string
  description = "New globally unique bucket in this project; shared by every janitor for the project."
}
variable "services" {
  type    = set(string)
  default = ["compute", "cloud_run", "gke", "scheduler", "legacy_functions"]
  validation {
    condition = alltrue([for s in var.services : contains(
    ["compute", "cloud_run", "gke", "scheduler", "legacy_functions"], s)])
    error_message = "Unsupported service."
  }
}
variable "dry_run" {
  type    = bool
  default = true
}
variable "mode" {
  type    = string
  default = "stop"
  validation {
    condition     = contains(["stop", "lifecycle"], var.mode)
    error_message = "mode must be stop or lifecycle."
  }
}
variable "ttl_hours" {
  type    = number
  default = 24
  validation {
    condition     = var.ttl_hours >= 1 / 60 && var.ttl_hours <= 87600
    error_message = "TTL must be between one minute and ten years."
  }
}
variable "grace_hours" {
  type    = number
  default = 24
  validation {
    condition     = var.grace_hours >= 1 / 60 && var.grace_hours <= 87600
    error_message = "Recovery grace must be between one minute and ten years."
  }
}
variable "max_actions_per_run" {
  type    = number
  default = 10
  validation {
    condition = (var.max_actions_per_run >= 1 && var.max_actions_per_run <= 1000 &&
    floor(var.max_actions_per_run) == var.max_actions_per_run)
    error_message = "Use an integer action cap from 1 to 1000."
  }
}
variable "max_actions_per_window" {
  type    = number
  default = 10
  validation {
    condition = (var.max_actions_per_window >= var.max_actions_per_run &&
    var.max_actions_per_window <= 1000 && floor(var.max_actions_per_window) == var.max_actions_per_window)
    error_message = "Use an integer shared cap at least as large as the run cap, no more than 1000."
  }
}
variable "window_seconds" {
  type    = number
  default = 3600
  validation {
    condition = (var.window_seconds >= 1 && var.window_seconds <= 86400 &&
    floor(var.window_seconds) == var.window_seconds)
    error_message = "Use an integer rolling budget window from 1 to 86400 seconds."
  }
}
variable "scheduler_jobs" {
  type = map(object({
    expires_at = optional(string)
    ttl_hours  = optional(number)
    owner      = optional(string)
    excluded   = optional(bool)
  }))
  default     = {}
  description = "Exact Scheduler job resource names mapped to operator-owned expiry settings."
}
variable "protected_resources" {
  type    = set(string)
  default = []
}
variable "cloud_run_service_accounts" {
  type        = set(string)
  default     = []
  description = "Exact service identity emails of enrolled Cloud Run services; never a project-wide actAs grant."
}
variable "cloud_run_image_repositories" {
  type        = set(string)
  default     = []
  description = "Optional exact projects/P/locations/L/repositories/R names for enrolled workload image read permissions."
  validation {
    condition = alltrue([for r in var.cloud_run_image_repositories :
    can(regex("^projects/[^/]+/locations/[^/]+/repositories/[^/]+$", r))])
    error_message = "Use complete Artifact Registry repository resource names."
  }
}
variable "schedule" {
  type    = string
  default = "*/15 * * * *"
}
variable "scheduler_paused" {
  type        = bool
  default     = true
  description = "Start paused; execute and review a dry run before enabling the timer."
}
variable "notification_channels" {
  type        = list(string)
  default     = []
  description = "Existing Cloud Monitoring notification-channel resource names."
}
