"""Small, explicit REST adapters using ADC. Mutations are never retried automatically."""

import hashlib
import json
import re
from urllib.parse import urlparse

import google.auth
from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage

from .config import ConfigError
from .state import GCSObjects, Store

BASES = {
    "compute": "https://compute.googleapis.com/compute/v1/",
    "cloud_run": "https://run.googleapis.com/v2/",
    "gke": "https://container.googleapis.com/v1/",
    "scheduler": "https://cloudscheduler.googleapis.com/v1/",
    "legacy_functions": "https://cloudfunctions.googleapis.com/v1/",
    "projects": "https://cloudresourcemanager.googleapis.com/v3/",
}


class APIError(RuntimeError):
    def __init__(self, status, message="GCP API request failed"):
        self.status = status
        super().__init__(message)


class GCP:
    def __init__(self, policy, session=None):
        self.policy = policy
        self.check_time = lambda: None
        if session is None:
            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            session = AuthorizedSession(credentials, max_refresh_attempts=0, refresh_timeout=10)
            self.objects = GCSObjects(
                storage.Client(project=policy.project_id, credentials=credentials).bucket(
                    policy.state_bucket
                )
            )
        self.session = session

    def store(self):
        self.objects.verify_project(self.policy.project_number)
        return Store(self.objects, self.policy)

    def call(self, kind, path, method="GET", body=None, params=None):
        self.check_time()
        # Paths are constructed from validated resource names, never from an arbitrary API URL.
        if not path.startswith(f"projects/{self.policy.project_id}/") and not (
            kind == "projects" and path == f"projects/{self.policy.project_id}"
        ):
            raise ConfigError("API path is outside the configured project")
        response = self.session.request(
            method,
            BASES[kind] + path,
            json=body,
            params=params,
            timeout=(3, 10),
            allow_redirects=False,
        )
        if not 200 <= response.status_code < 300:
            raise APIError(response.status_code)
        return response.json()

    def verify_project(self):
        project = self.call("projects", f"projects/{self.policy.project_id}")
        if (
            project.get("projectId") != self.policy.project_id
            or project.get("name") != f"projects/{self.policy.project_number}"
            or project.get("state") != "ACTIVE"
        ):
            raise ConfigError("Project ID/number/state does not match the operator policy")

    def pages(self, kind, path, key):
        params = {}
        seen = set()
        while True:
            page = self.call(kind, path, params=params)
            if page.get("unreachable") or page.get("missingZones"):
                raise APIError(503, "Incomplete discovery")
            warning = page.get("warning", {}).get("code")
            if warning and warning != "NO_RESULTS_ON_PAGE":
                raise APIError(503, "Discovery warning")
            yield from page.get(key, [])
            token = page.get("nextPageToken")
            if not token:
                return
            if token in seen:
                raise APIError(503, "Repeated pagination token")
            seen.add(token)
            params = {"pageToken": token}

    def discover(self, check_time):
        self.check_time = check_time
        p = f"projects/{self.policy.project_id}"
        if "compute" in self.policy.services:
            for zone in self.policy.zones:
                for raw in self.pages("compute", f"{p}/zones/{zone}/instances", "items"):
                    if raw.get("labels", {}).get("janitor-managed") == "true":
                        yield self.normalize(
                            "compute", f"{p}/zones/{zone}/instances/{raw['name']}", raw
                        )
        for region in self.policy.regions:
            parent = f"{p}/locations/{region}"
            for kind, plural, key in (
                ("cloud_run", "services", "services"),
                ("legacy_functions", "functions", "functions"),
            ):
                if kind in self.policy.services:
                    for raw in self.pages(kind, f"{parent}/{plural}", key):
                        if raw.get("labels", {}).get("janitor-managed") == "true":
                            yield self.normalize(kind, raw["name"], raw)
        if "gke" in self.policy.services:
            for location in (*self.policy.regions, *self.policy.zones):
                parent = f"{p}/locations/{location}"
                for cluster in self.pages("gke", f"{parent}/clusters", "clusters"):
                    cluster_name = f"{parent}/clusters/{cluster['name']}"
                    pools = self.call("gke", cluster_name + "/nodePools")
                    for pool in pools.get("nodePools", []):
                        if (
                            pool.get("config", {}).get("resourceLabels", {}).get("janitor-managed")
                            == "true"
                        ):
                            yield self.normalize(
                                "gke", cluster_name + "/nodePools/" + pool["name"], pool, cluster
                            )
        if "scheduler" in self.policy.services:
            # Scheduler jobs have no resource labels: enrollment is an operator-owned exact list.
            for name in self.policy.scheduler_jobs:
                yield self.refresh("scheduler", name)

    def refresh(self, kind, name):
        self.policy.validate_name(name, kind)
        raw = self.call(kind, name)
        if kind != "compute" and kind != "gke" and raw.get("name") != name:
            raise ConfigError("API returned a different resource")
        cluster = self.call("gke", name.split("/nodePools/")[0]) if kind == "gke" else None
        return self.normalize(kind, name, raw, cluster)

    def normalize(self, kind, name, raw, cluster=None):
        self.policy.validate_name(name, kind)
        labels = raw.get("labels", {})
        identity = raw.get("uid", raw.get("id", name))
        snapshot = {}
        protection = None
        status = "active"
        if kind == "compute":
            if raw.get("name") != name.rsplit("/", 1)[-1]:
                raise ConfigError("Unexpected compute instance name")
            status = {"RUNNING": "active", "TERMINATED": "inactive"}.get(
                raw.get("status"), "transitioning"
            )
            snapshot = {
                "status": raw.get("status"),
                "last_start": raw.get("lastStartTimestamp"),
                "last_stop": raw.get("lastStopTimestamp"),
                "label_fingerprint": raw.get("labelFingerprint"),
                "disks": [
                    {k: d.get(k) for k in ("source", "autoDelete", "type", "deviceName")}
                    for d in raw.get("disks", [])
                ],
                "deletion_protection": raw.get("deletionProtection", False),
            }
            metadata = {v["key"]: v.get("value") for v in raw.get("metadata", {}).get("items", [])}
            if metadata.get("created-by") or any(
                k.startswith(("goog-gke-", "goog-dataproc-")) for k in labels
            ):
                protection = "managed_instance"
            elif (
                raw.get("scheduling", {}).get("preemptible")
                or raw.get("scheduling", {}).get("provisioningModel") == "SPOT"
            ):
                protection = "spot_or_preemptible"
            elif any(d.get("type") == "SCRATCH" for d in raw.get("disks", [])):
                protection = "local_ssd"
            elif raw.get("resourcePolicies"):
                protection = "instance_schedule_attached"
        elif kind == "cloud_run":
            snapshot = {
                "scaling": raw.get("scaling", {}),
                "etag": raw.get("etag"),
                "generation": raw.get("generation"),
                "reconciling": raw.get("reconciling", False),
            }
            if (
                raw.get("reconciling")
                or raw.get("terminalCondition", {}).get("state") != "CONDITION_SUCCEEDED"
            ):
                status = "transitioning"
            elif (
                snapshot["scaling"].get("scalingMode") == "MANUAL"
                and snapshot["scaling"].get("manualInstanceCount", 0) == 0
            ):
                status = "inactive"
            if not raw.get("etag"):
                protection = "missing_etag"
        elif kind == "gke":
            labels = raw.get("config", {}).get("resourceLabels", {})
            counts = {}
            for url in raw.get("instanceGroupUrls", []):
                parsed = urlparse(url)
                if parsed.scheme != "https" or parsed.netloc not in (
                    "www.googleapis.com",
                    "compute.googleapis.com",
                ):
                    raise ConfigError("Unexpected instance group URL")
                path = parsed.path.removeprefix("/compute/v1/").replace(
                    "/instanceGroups/", "/instanceGroupManagers/"
                )
                if not re.fullmatch(
                    rf"projects/{re.escape(self.policy.project_id)}/zones/[a-z0-9-]+/instanceGroupManagers/[a-z0-9-]+",
                    path,
                ):
                    raise ConfigError("Instance group is outside project scope")
                if path.split("/")[3].rsplit("-", 1)[0] not in self.policy.regions:
                    raise ConfigError("Instance group is outside configured regions")
                group = self.call("compute", path)
                counts[path] = group["targetSize"]
                if any(
                    value for key, value in group.get("currentActions", {}).items() if key != "none"
                ):
                    protection = "node_group_transitioning"
            identity = sorted(counts)
            snapshot = {
                "counts_per_zone": counts,
                "autoscaling": raw.get("autoscaling", {}),
                "cluster_autoscaling": cluster.get("autoscaling", {}),
                "status": raw.get("status"),
                "autopilot": cluster.get("autopilot", {}).get("enabled", False),
            }
            if not counts or raw.get("status") != "RUNNING" or cluster.get("status") != "RUNNING":
                status = "transitioning"
            elif all(count == 0 for count in counts.values()):
                status = "inactive"
            if snapshot["autopilot"]:
                protection = "autopilot_cluster"
            elif snapshot["autoscaling"].get("enabled") or snapshot["cluster_autoscaling"].get(
                "enableNodeAutoprovisioning"
            ):
                protection = "autoscaling_enabled"
            elif labels.get("janitor-allow-disruption") != "true":
                protection = "disruption_not_approved"
        elif kind == "scheduler":
            enrollment = self.policy.scheduler_jobs[name]
            labels = {
                "janitor-managed": "true",
                "janitor-ttl-hours": str(enrollment.get("ttl_hours", self.policy.ttl_hours)),
                "owner": enrollment.get("owner", ""),
            }
            if enrollment.get("excluded"):
                labels["do-not-cleanup"] = "true"
            if "expires_at" in enrollment:
                snapshot["expires_at"] = enrollment["expires_at"]
            snapshot.update(state=raw.get("state"), user_update_time=raw.get("userUpdateTime"))
            status = {"ENABLED": "active", "PAUSED": "inactive"}.get(
                raw.get("state"), "transitioning"
            )
        else:
            protection = "first_generation_function_has_no_pause"
            snapshot = {"status": raw.get("status")}
        return {
            "id": name,
            "kind": kind,
            "identity": identity,
            "labels": labels,
            "status": status,
            "snapshot": snapshot,
            "protection": protection,
        }

    def protection(self, resource, action):
        if resource["protection"]:
            return resource["protection"]
        if resource["kind"] == "compute":
            if any(self.pages("compute", resource["id"] + "/referrers", "items")):
                return "referenced_instance"
            if action == "delete" and resource["snapshot"]["deletion_protection"]:
                return "deletion_protected"
        return None

    def act(self, resource, action, token):
        kind, name = resource["kind"], resource["id"]
        if action in ("stop", "delete"):
            response = self.call(
                kind,
                name + ("/stop" if action == "stop" else ""),
                "POST" if action == "stop" else "DELETE",
                params={"requestId": token},
            )
            operation_path = name.rsplit("/instances/", 1)[0] + "/operations/" + response["name"]
        elif action == "disable_service":
            response = self.call(
                kind,
                name,
                "PATCH",
                body={
                    "name": name,
                    "etag": resource["snapshot"]["etag"],
                    "scaling": {"scalingMode": "MANUAL", "manualInstanceCount": 0},
                },
                params={"updateMask": "scaling.scalingMode,scaling.manualInstanceCount"},
            )
            operation_path = response["name"]
        elif action == "scale_pool_zero":
            response = self.call(kind, name + ":setSize", "POST", body={"nodeCount": 0})
            operation_path = name.split("/clusters/")[0] + "/operations/" + response["name"]
        elif action == "pause_job":
            response = self.call(kind, name + ":pause", "POST", body={})
            if response.get("state") != "PAUSED":
                raise APIError(500, "Scheduler did not acknowledge pause")
            return None
        else:
            raise ValueError("Unsupported action")
        self.validate_operation(kind, operation_path)
        # Accepted operations remain pending until a later invocation confirms completion.
        return {"kind": kind, "path": operation_path}

    def validate_operation(self, kind, path):
        location_type = "zones" if kind == "compute" else "locations"
        if not re.fullmatch(
            rf"projects/{re.escape(self.policy.project_id)}/{location_type}/[a-z0-9-]+/operations/[a-zA-Z0-9_-]+",
            path,
        ):
            raise APIError(500, "Unrecognized operation name")
        location = path.split("/")[3]
        if location not in (*self.policy.regions, *self.policy.zones):
            raise ConfigError("Operation is outside configured locations")

    def poll(self, operation):
        self.validate_operation(operation["kind"], operation["path"])
        result = self.call(operation["kind"], operation["path"])
        done = (
            result.get("done", False)
            if operation["kind"] == "cloud_run"
            else result.get("status") == "DONE"
        )
        if not done:
            return "pending"
        return "failed" if result.get("error") or result.get("errorMessage") else "succeeded"


def fingerprint(resource, stop_authority=False):
    value = {
        k: resource[k] for k in ("id", "identity", "labels", "snapshot", "protection", "status")
    }
    if stop_authority:
        value["snapshot"] = {
            k: v for k, v in resource["snapshot"].items() if k not in ("status", "last_stop")
        }
        value.pop("status")
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
