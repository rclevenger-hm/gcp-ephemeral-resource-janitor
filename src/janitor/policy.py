"""Labels use a compact UTC expiry because GCP label values cannot contain colons."""

import math
from datetime import datetime, timedelta, timezone

from .config import iso, timestamp
from .gcp import fingerprint


def evaluate(resource, state, policy, now):
    labels = resource["labels"]
    if resource["id"] in policy.protected_resources:
        return None, "janitor_infrastructure"
    if labels.get("janitor-managed") != "true":
        return None, "not_opted_in"
    if "do-not-cleanup" in labels:
        return None, "excluded"
    if resource.get("protection"):
        return None, resource["protection"]
    if state.get("pending") or state.get("blocked"):
        return None, "unresolved_action"
    if resource["status"] == "transitioning":
        return None, "transitioning"
    try:
        if "expires_at" in resource["snapshot"]:
            expiry = timestamp(resource["snapshot"]["expires_at"])
        elif "janitor-expires-at" in labels:
            expiry = datetime.strptime(labels["janitor-expires-at"], "%Y-%m-%dt%H%M%Sz").replace(
                tzinfo=timezone.utc
            )
        else:
            ttl = float(labels.get("janitor-ttl-hours", policy.ttl_hours))
            if not math.isfinite(ttl) or not 0 < ttl <= 87600:
                return None, "invalid_ttl"
            expiry = timestamp(state["first_seen_at"]) + timedelta(hours=ttl)
    except (TypeError, ValueError, OverflowError, KeyError):
        return None, "invalid_expiry"
    if expiry > now:
        state.pop("stop", None)
        state.pop("quarantine", None)
        return None, "not_expired"
    if resource["kind"] != "compute":
        if resource["status"] == "inactive":
            return None, "already_inactive"
        if state.get("quarantine"):
            return None, "quarantine_submitted_or_manually_restored"
        return {"cloud_run": "disable_service", "gke": "scale_pool_zero", "scheduler": "pause_job"}[
            resource["kind"]
        ], "expired"
    history = state.get("stop")
    if history and history["fingerprint"] != fingerprint(resource, stop_authority=True):
        state.pop("stop", None)
        history = None
    if state.get("deleted"):
        return None, "deletion_confirmed"
    if resource["status"] == "active":
        if history:
            if history.get("observed_stopped_at"):
                state.pop("stop", None)
                return None, "restart_history_reset"
            return None, "stop_confirmed_waiting_for_state"
        return "stop", "expired"
    if policy.mode != "lifecycle":
        return None, "already_stopped"
    if labels.get("janitor-allow-delete") != "true":
        return None, "deletion_not_opted_in"
    if not history:
        return None, "no_confirmed_janitor_stop"
    token = fingerprint(resource)
    if history.get("stopped_fingerprint") != token:
        history.update(stopped_fingerprint=token, observed_stopped_at=iso(now))
    if (
        now - timestamp(history["observed_stopped_at"])
    ).total_seconds() < policy.grace_hours * 3600:
        return None, "recovery_grace"
    return "delete", "grace_elapsed"
