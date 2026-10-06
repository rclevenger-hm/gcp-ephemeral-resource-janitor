"""Only operator configuration can grant authority; requests can only narrow it."""

import json
import math
import os
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone


class ConfigError(ValueError):
    pass


def timestamp(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("Expected an ISO timestamp")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Timestamp must include timezone")
    return result.astimezone(timezone.utc)


def iso(value):
    return timestamp(value).isoformat()


def number(value, name, low, high, integer=False):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ConfigError(f"{name} must be a number")
    if not low <= value <= high or not math.isfinite(value):
        raise ConfigError(f"{name} must be between {low} and {high}")
    if integer and not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer")
    return value


@dataclass(frozen=True)
class Policy:
    project_id: str
    project_number: str
    state_bucket: str
    regions: tuple
    zones: tuple = ()
    services: tuple = ("compute", "cloud_run", "gke", "scheduler", "legacy_functions")
    dry_run: bool = True
    mode: str = "stop"
    ttl_hours: float = 24
    grace_hours: float = 24
    max_actions_per_run: int = 10
    max_actions_per_window: int = 10
    window_seconds: int = 3600
    max_resources: int = 1000
    runtime_seconds: int = 480
    scheduler_jobs: dict = field(default_factory=dict)
    protected_resources: tuple = ()

    def __post_init__(self):
        if not isinstance(self.project_id, str) or not re.fullmatch(
            r"[a-z][a-z0-9-]{4,28}[a-z0-9]", self.project_id
        ):
            raise ConfigError("project_id must be an explicit GCP project ID")
        if not isinstance(self.project_number, str) or not re.fullmatch(
            r"[1-9][0-9]+", self.project_number
        ):
            raise ConfigError("project_number must be the numeric project number as a string")
        if not isinstance(self.state_bucket, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9.-]{1,220}[a-z0-9]", self.state_bucket
        ):
            raise ConfigError("state_bucket must be a bucket name")
        for key, pattern in (
            ("regions", r"[a-z]+-[a-z]+[0-9]+"),
            ("zones", r"[a-z]+-[a-z]+[0-9]+-[a-z]"),
        ):
            values = getattr(self, key)
            if not isinstance(values, (list, tuple)) or any(
                not isinstance(v, str) or not re.fullmatch(pattern, v) for v in values
            ):
                raise ConfigError(f"{key} must be an explicit list")
        if not self.regions or any(z.rsplit("-", 1)[0] not in self.regions for z in self.zones):
            raise ConfigError("zones must belong to configured regions")
        allowed = {"compute", "cloud_run", "gke", "scheduler", "legacy_functions"}
        if not isinstance(self.services, (list, tuple)) or any(
            s not in allowed for s in self.services
        ):
            raise ConfigError("Unsupported service")
        if type(self.dry_run) is not bool or self.mode not in ("stop", "lifecycle"):
            raise ConfigError("dry_run must be boolean; mode must be stop or lifecycle")
        for key in ("ttl_hours", "grace_hours"):
            number(getattr(self, key), key, 1 / 60, 87600)
        for key, upper in (
            ("max_actions_per_run", 1000),
            ("max_actions_per_window", 1000),
            ("window_seconds", 86400),
            ("max_resources", 10000),
            ("runtime_seconds", 480),
        ):
            number(getattr(self, key), key, 1, upper, integer=True)
        if self.runtime_seconds < 120 or self.max_actions_per_run > self.max_actions_per_window:
            raise ConfigError("Invalid runtime or per-run/window action limits")
        if not isinstance(self.scheduler_jobs, dict):
            raise ConfigError("scheduler_jobs must map exact resource names to enrollment settings")
        for name, enrollment in self.scheduler_jobs.items():
            self.validate_name(name, "scheduler")
            if not isinstance(enrollment, dict) or enrollment.keys() - {
                "expires_at",
                "ttl_hours",
                "owner",
                "excluded",
            }:
                raise ConfigError("Invalid Scheduler enrollment")
            if "expires_at" in enrollment:
                try:
                    timestamp(enrollment["expires_at"])
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ConfigError("Invalid Scheduler expiry") from exc
            if "ttl_hours" in enrollment:
                number(enrollment["ttl_hours"], "ttl_hours", 1 / 60, 87600)
            if type(enrollment.get("excluded", False)) is not bool:
                raise ConfigError("Scheduler excluded must be boolean")
        if not isinstance(self.protected_resources, (tuple, list)) or any(
            not isinstance(v, str) or not v.startswith(f"projects/{self.project_id}/")
            for v in self.protected_resources
        ):
            raise ConfigError("Protected resources must belong to the configured project")

    def validate_name(self, name, kind):
        patterns = {
            "compute": r"zones/([^/]+)/instances/([a-zA-Z0-9_-]+)",
            "cloud_run": r"locations/([^/]+)/services/([a-zA-Z0-9_-]+)",
            "gke": r"locations/([^/]+)/clusters/([a-zA-Z0-9_-]+)/nodePools/([a-zA-Z0-9_-]+)",
            "scheduler": r"locations/([^/]+)/jobs/([a-zA-Z0-9_-]+)",
            "legacy_functions": r"locations/([^/]+)/functions/([a-zA-Z0-9_-]+)",
        }
        match = re.fullmatch(rf"projects/{re.escape(self.project_id)}/{patterns[kind]}", name)
        locations = (
            self.zones
            if kind == "compute"
            else (*self.regions, *self.zones)
            if kind == "gke"
            else self.regions
        )
        if not match or match[1] not in locations:
            raise ConfigError("Resource outside operator project/location scope")

    def public(self):
        return json.loads(json.dumps(asdict(self)))


def load_policy(environ=None):
    env = os.environ if environ is None else environ
    try:
        value = json.loads(env["JANITOR_POLICY"])
        if not isinstance(value, dict):
            raise ConfigError("JANITOR_POLICY must be an object")
        return Policy(**value)
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid operator policy: {exc}") from exc


def narrow(policy, request):
    if not isinstance(request, dict) or request.keys() - {
        "dry_run",
        "max_actions_per_run",
        "resource_ids",
    }:
        raise ConfigError("Only dry_run, max_actions_per_run and resource_ids are accepted")
    changes = {}
    if "dry_run" in request:
        if type(request["dry_run"]) is not bool or (policy.dry_run and not request["dry_run"]):
            raise ConfigError("Requests cannot disable operator dry_run")
        changes["dry_run"] = request["dry_run"]
    if "max_actions_per_run" in request:
        changes["max_actions_per_run"] = number(
            request["max_actions_per_run"],
            "max_actions_per_run",
            1,
            policy.max_actions_per_run,
            integer=True,
        )
    if "resource_ids" in request:
        ids = request["resource_ids"]
        if (
            not isinstance(ids, list)
            or len(ids) > policy.max_resources
            or any(not isinstance(v, str) or not 1 <= len(v) <= 2048 for v in ids)
        ):
            raise ConfigError("resource_ids must be a bounded list of resource names")
    return replace(policy, **changes)
