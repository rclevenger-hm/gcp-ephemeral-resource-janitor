from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import NOW

from janitor.config import ConfigError, iso, load_policy, narrow
from janitor.gcp import fingerprint
from janitor.policy import evaluate


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "lifecycle"},
        {"project_id": "different-project"},
        {"zones": ["us-east1-a"]},
        {"grace_hours": 0},
        {"protected_resources": []},
        {"services": []},
        {"max_actions_per_window": 999},
        {"max_actions_per_run": 0},
        {"max_actions_per_run": 11},
        {"max_actions_per_run": True},
        {"dry_run": "false"},
        {"resource_ids": "all"},
    ],
)
def test_requests_cannot_expand_authority(policy, payload):
    with pytest.raises(ConfigError):
        narrow(policy, payload)


def test_requests_can_only_enable_dryrun_or_reduce_cap(policy):
    assert narrow(policy, {"dry_run": True, "max_actions_per_run": 1}).dry_run
    with pytest.raises(ConfigError):
        narrow(replace(policy, dry_run=True), {"dry_run": False})


@pytest.mark.parametrize(
    "field,value",
    [
        ("ttl_hours", float("nan")),
        ("grace_hours", float("inf")),
        ("ttl_hours", 1e100),
        ("max_actions_per_run", -1),
        ("runtime_seconds", 600),
        ("regions", "*"),
        ("zones", ("europe-west1-a",)),
        ("project_number", "wrong"),
    ],
)
def test_operator_validation(policy, field, value):
    with pytest.raises(ConfigError):
        replace(policy, **{field: value})


@pytest.mark.parametrize("ttl", ["NaN", "Infinity", "-Infinity", "1e100", "0", "-3", "", "garbage"])
def test_malformed_resource_ttl_is_ineligible(policy, cloud, ttl):
    r = next(iter(cloud.resources.values()))
    r["labels"].pop("janitor-expires-at")
    r["labels"]["janitor-ttl-hours"] = ttl
    action, reason = evaluate(r, {"first_seen_at": iso(NOW)}, policy, NOW)
    assert action is None and reason in ("invalid_ttl", "invalid_expiry")


def test_ttl_uses_first_seen_and_compact_expiry(policy, cloud):
    r = next(iter(cloud.resources.values()))
    assert evaluate(r, {}, policy, NOW)[0] == "stop"
    r["labels"].pop("janitor-expires-at")
    s = {"first_seen_at": iso(NOW)}
    assert evaluate(r, s, policy, NOW)[1] == "not_expired"
    assert evaluate(r, s, policy, NOW + timedelta(hours=25))[0] == "stop"


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda r: r["labels"].update({"do-not-cleanup": "false"}), "excluded"),
        (lambda r: r["labels"].pop("janitor-managed"), "not_opted_in"),
        (lambda r: r.update(status="transitioning"), "transitioning"),
    ],
)
def test_protection_labels_fail_closed(policy, cloud, mutate, reason):
    r = next(iter(cloud.resources.values()))
    mutate(r)
    assert evaluate(r, {}, policy, NOW) == (None, reason)


def test_stopped_vm_needs_our_confirmed_stop_and_grace(policy, cloud):
    policy = replace(policy, mode="lifecycle")
    r = next(iter(cloud.resources.values()))
    r["labels"]["janitor-allow-delete"] = "true"
    s = {"stop": {"fingerprint": fingerprint(r, stop_authority=True)}}
    r.update(status="inactive")
    r["snapshot"]["status"] = "TERMINATED"
    r["snapshot"]["last_stop"] = iso(NOW)
    assert evaluate(r, {}, policy, NOW)[1] == "no_confirmed_janitor_stop"
    assert evaluate(r, s, policy, NOW)[1] == "recovery_grace"
    assert evaluate(r, s, policy, NOW + timedelta(hours=23))[0] is None
    assert evaluate(r, s, policy, NOW + timedelta(hours=24))[0] == "delete"
    r["snapshot"]["last_start"] = iso(NOW)
    assert evaluate(r, s, policy, NOW + timedelta(hours=25))[1] == "no_confirmed_janitor_stop"


def test_extension_revokes_previous_stop_authority(policy, cloud):
    r = next(iter(cloud.resources.values()))
    r["labels"]["janitor-expires-at"] = "2027-01-01t000000z"
    s = {"stop": {}, "quarantine": {}}
    evaluate(r, s, policy, NOW)
    assert s == {}


def test_policy_is_not_read_from_untrusted_request_environment():
    with pytest.raises(ConfigError):
        load_policy({"JANITOR_POLICY": "[]"})
