import copy
from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import NOW, Cloud, vm

from janitor.config import iso
from janitor.engine import run
from janitor.gcp import APIError
from janitor.state import RunLocked, StateError, Store


def execute(policy, cloud, store, **kwargs):
    return run(policy, {}, cloud, store, clock=lambda: NOW, **kwargs)


def test_plan_and_intent_are_durable_before_any_mutation(policy, cloud, store, objects):
    def check(resource):
        state, _ = store.read("state.json")
        report, _ = store.read(f"runs/{store.run_id}.json")
        assert len(report["resources"]) == 1
        assert report["resources"][0]["outcome"] == "pending"
        assert state["resources"][resource["id"]]["pending"]["action"] == "stop"
        assert len(state["reservations"]) == 1

    cloud.act_hook = check
    result = execute(policy, cloud, store)
    assert result["status"] == "complete"
    assert result["counts"] == {"submitted": 1}
    assert store.read("lock.json")[0] is None


@pytest.mark.parametrize(
    "code,expected_actions,status",
    [(400, 3, "partial"), (403, 2, "failed"), (503, 2, "failed"), (429, 2, "failed")],
)
def test_partial_results_survive_action_failure(policy, store, code, expected_actions, status):
    cloud = Cloud(policy, [vm("vm-a"), vm("vm-b"), vm("vm-c")])
    names = list(cloud.resources)
    cloud.failures[names[1]] = APIError(code)
    result = execute(policy, cloud, store)
    assert result["status"] == status
    assert len(cloud.actions) == expected_actions
    durable, _ = store.read(f"runs/{result['run_id']}.json")
    assert durable["resources"][0]["outcome"] == "submitted"
    state, _ = store.read("state.json")
    assert ("pending" in state["resources"][names[1]]) == (code in (503, 429))


def test_unknown_action_is_not_repeated_on_next_run(policy, cloud, store):
    name = next(iter(cloud.resources))
    cloud.failures[name] = TimeoutError()
    assert execute(policy, cloud, store)["status"] == "failed"
    cloud.failures.clear()
    assert execute(policy, cloud, store)["status"] == "partial"
    assert len(cloud.actions) == 1


@pytest.mark.parametrize("checkpoint", ["plan", "intent", "outcome", "state_outcome"])
def test_checkpoint_failure_retains_lock_and_blocks_later_workers(
    policy, cloud, store, objects, checkpoint
):
    def fail(key, body):
        if checkpoint == "plan":
            return "/runs/" in key and body["status"] == "executing"
        if checkpoint == "intent":
            return key.endswith("state.json") and any(
                "pending" in e for e in body["resources"].values()
            )
        if checkpoint == "outcome":
            return "/runs/" in key and any(p["outcome"] == "submitted" for p in body["resources"])
        return key.endswith("state.json") and any(
            "operation" in e for e in body["resources"].values()
        )

    objects.fail = fail
    with pytest.raises(StateError):
        execute(policy, cloud, store)
    assert store.read("lock.json")[0]
    assert len(cloud.actions) == (checkpoint in ("outcome", "state_outcome"))
    objects.fail = lambda key, body: False
    with pytest.raises(RunLocked):
        execute(policy, cloud, Store(objects, policy))


def test_two_workers_cannot_share_lock(policy, cloud, store, objects):
    store.acquire("a" * 32, iso(NOW))
    with pytest.raises(RunLocked):
        execute(policy, cloud, Store(objects, policy))
    assert not cloud.actions


def test_lock_generation_change_fences_checkpoints(policy, store, objects):
    state = store.acquire("a" * 32, iso(NOW))
    objects.write(store.prefix + "lock.json", {"run_id": "a" * 32}, store.lock_generation)
    with pytest.raises(StateError):
        store.save_state(state)


def test_completed_execution_is_not_replayed(policy, cloud, store):
    first = execute(policy, cloud, store, run_id="a" * 32)
    duplicate = execute(policy, cloud, store, run_id="a" * 32)
    assert duplicate["duplicate"] and duplicate["run_id"] == first["run_id"]
    assert len(cloud.actions) == 1


def test_shared_budget_applies_across_independent_workers(policy, store, objects):
    policy = replace(policy, max_actions_per_run=1, max_actions_per_window=1)
    one = Cloud(policy, [vm("vm-a")])
    two = Cloud(policy, [vm("vm-b")])
    two.resources.update(one.resources)
    execute(policy, one, store)
    result = execute(policy, two, Store(objects, policy))
    assert result["reasons"] == {
        "shared_budget_exhausted": 1,
        "stop_confirmed_waiting_for_state": 1,
    }
    assert not two.actions


def test_window_expiry_allows_new_action(policy, cloud, store):
    policy = replace(policy, max_actions_per_run=1, max_actions_per_window=1)
    execute(policy, cloud, store)
    name = next(iter(cloud.resources))
    cloud.resources[name]["snapshot"]["last_start"] = iso(NOW + timedelta(hours=2))
    run(policy, {}, cloud, store, clock=lambda: NOW + timedelta(hours=2))
    assert len(cloud.actions) == 2


def test_budget_policy_change_requires_reconciliation(policy, cloud, store):
    execute(policy, cloud, store)
    result = execute(replace(policy, window_seconds=7200), cloud, store)
    assert result["error"] == "budget_policy_changed"
    assert len(cloud.actions) == 1


def test_dryrun_has_no_workload_mutations_or_budget_reservations(policy, cloud, store):
    result = run(policy, {"dry_run": True}, cloud, store, clock=lambda: NOW)
    assert result["counts"] == {"would_act": 1}
    assert not cloud.actions
    assert store.read("state.json")[0]["reservations"] == []


def test_changed_labels_between_plan_and_action_are_rejected(policy, cloud, store):
    calls = 0

    def change(resource):
        nonlocal calls
        calls += 1
        if calls >= 2:
            resource["labels"]["do-not-cleanup"] = "true"
        return resource

    cloud.refresh_hook = change
    result = execute(policy, cloud, store)
    assert result["reasons"] == {"changed_before_action": 1}
    assert not cloud.actions


def test_full_discovery_must_succeed_before_mutation(policy, cloud, store):
    cloud.discovery_error = APIError(503)
    assert execute(policy, cloud, store)["status"] == "failed"
    assert not cloud.actions


def test_discovery_bound_fails_without_actions(policy, store):
    cloud = Cloud(policy, [vm("vm-a"), vm("vm-b")])
    assert execute(replace(policy, max_resources=1), cloud, store)["status"] == "failed"
    assert not cloud.actions


def test_malformed_ttl_does_not_block_other_resources(policy, store):
    bad = vm("vm-a")
    bad["labels"].pop("janitor-expires-at")
    bad["labels"]["janitor-ttl-hours"] = "NaN"
    cloud = Cloud(policy, [bad, vm("vm-b")])
    result = execute(policy, cloud, store)
    assert result["counts"] == {"skipped": 1, "submitted": 1}
    assert len(cloud.actions) == 1


def test_request_subset_excludes_every_other_resource(policy, store):
    cloud = Cloud(policy, [vm("vm-a"), vm("vm-b")])
    name = next(iter(cloud.resources))
    run(policy, {"resource_ids": [name]}, cloud, store, clock=lambda: NOW)
    assert [a[0] for a in cloud.actions] == [name]


@pytest.mark.parametrize(
    "operation_state,reason", [("pending", "operation_in_progress"), ("failed", "operation_failed")]
)
def test_operations_must_complete_before_lifecycle_progress(
    policy, cloud, store, operation_state, reason
):
    execute(policy, cloud, store)
    cloud.poll_result = operation_state
    result = execute(policy, cloud, store)
    assert result["reasons"] == {reason: 1}
    assert len(cloud.actions) == 1


def test_end_to_end_stop_confirm_grace_delete(policy, cloud, store):
    policy = replace(policy, mode="lifecycle")
    r = next(iter(cloud.resources.values()))
    r["labels"]["janitor-allow-delete"] = "true"
    execute(policy, cloud, store)
    r["status"] = "inactive"
    r["snapshot"].update(status="TERMINATED", last_stop=iso(NOW))
    assert execute(policy, cloud, store)["reasons"] == {"recovery_grace": 1}
    run(policy, {}, cloud, store, clock=lambda: NOW + timedelta(hours=25))
    assert [a[1] for a in cloud.actions] == ["stop", "delete"]
    cloud.resources.clear()
    result = run(policy, {}, cloud, store, clock=lambda: NOW + timedelta(hours=26))
    assert result["reasons"] == {"deletion_confirmed": 1}
    assert not store.read("state.json")[0]["resources"][r["id"]].get("operation")


def test_history_does_not_expand_current_service_scope(policy, cloud, store):
    execute(policy, cloud, store)
    result = execute(replace(policy, services=()), cloud, store)
    assert result["reasons"] == {"service_no_longer_enabled": 1}
    assert len(cloud.actions) == 1


def test_deadline_stops_before_first_api_mutation(policy, cloud, store):
    calls = 0

    def remaining():
        nonlocal calls
        calls += 1
        return 480 if calls < 4 else 30

    result = execute(policy, cloud, store, remaining=remaining)
    assert result["status"] == "failed"
    assert not cloud.actions


def test_failed_report_does_not_erase_existing_durable_partial_plan(policy, cloud, store, objects):
    snapshots = []

    def fail(key, body):
        if "/runs/" in key:
            snapshots.append(copy.deepcopy(body))
            return any(p["outcome"] == "submitted" for p in body["resources"])
        return False

    objects.fail = fail
    with pytest.raises(StateError):
        execute(policy, cloud, store)
    report, _ = store.read(f"runs/{store.run_id}.json")
    assert report["resources"][0]["outcome"] == "pending"
    assert store.read("state.json")[0]["resources"][next(iter(cloud.resources))]["pending"]


@pytest.mark.parametrize(
    "kind,action",
    [("cloud_run", "disable_service"), ("gke", "scale_pool_zero"), ("scheduler", "pause_job")],
)
def test_non_vm_quarantine_is_not_repeated_after_operator_restore(policy, store, kind, action):
    from conftest import PREFIX

    cloud = Cloud(policy)
    cloud.resources.clear()
    suffix = {
        "cloud_run": "services/app",
        "gke": "clusters/c/nodePools/p",
        "scheduler": "jobs/trigger",
    }[kind]
    name = f"{PREFIX}/locations/us-central1/{suffix}"
    cloud.resources[name] = dict(
        id=name,
        kind=kind,
        identity="resource-1",
        status="active",
        protection=None,
        labels={"janitor-managed": "true", "janitor-expires-at": "2026-10-01t000000z"},
        snapshot={},
    )
    execute(policy, cloud, store)
    assert cloud.actions[0][1] == action
    result = execute(policy, cloud, store)
    assert result["reasons"] == {"quarantine_submitted_or_manually_restored": 1}
    assert len(cloud.actions) == 1
    cloud.resources[name]["labels"]["janitor-expires-at"] = "2026-10-07t000000z"
    execute(policy, cloud, store)
    run(policy, {}, cloud, store, clock=lambda: NOW + timedelta(days=2))
    assert len(cloud.actions) == 2


def test_schedule_is_paused_before_compute_and_services(policy, store):
    from conftest import PREFIX

    cloud = Cloud(policy)
    for kind, suffix in [("cloud_run", "services/app"), ("scheduler", "jobs/trigger")]:
        name = f"{PREFIX}/locations/us-central1/{suffix}"
        cloud.resources[name] = dict(
            id=name,
            kind=kind,
            identity="resource-1",
            status="active",
            protection=None,
            labels={"janitor-managed": "true", "janitor-expires-at": "2026-10-01t000000z"},
            snapshot={},
        )
    execute(policy, cloud, store)
    assert [action for _, action, _ in cloud.actions] == ["pause_job", "disable_service", "stop"]
