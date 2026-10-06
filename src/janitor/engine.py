"""Persist a complete plan, reserve intent and budget, act, then reconcile operations."""

import copy
import time
import uuid
from collections import Counter
from datetime import datetime, timezone

from .config import iso, narrow, timestamp
from .gcp import APIError, fingerprint
from .policy import evaluate
from .state import StateError

RESOURCE_REJECTIONS = {400, 404, 409, 412, 422}


class Deadline(RuntimeError):
    pass


def run(operator, request, gcp, store, *, run_id=None, clock=None, remaining=None):
    policy = narrow(operator, request)
    clock = clock or (lambda: datetime.now(timezone.utc))
    deadline = time.monotonic() + policy.runtime_seconds
    remaining = remaining or (lambda: deadline - time.monotonic())
    run_id = run_id or uuid.uuid4().hex
    if len(run_id) != 32 or any(c not in "0123456789abcdef" for c in run_id):
        raise ValueError("run_id must be a UUID hex string")

    def check_time():
        if remaining() < 90:
            raise Deadline("Execution deadline reserve reached")

    check_time()
    gcp.check_time = check_time
    gcp.verify_project()
    previous, _ = store.read(f"runs/{run_id}.json")
    if previous and previous["status"] in ("complete", "partial", "failed"):
        return {**previous, "duplicate": True}
    now = clock()
    state = store.acquire(run_id, iso(now))
    report = {
        "schema": 1,
        "run_id": run_id,
        "project_id": policy.project_id,
        "started_at": iso(now),
        "status": "discovering",
        "dry_run": policy.dry_run,
        "policy": policy.public(),
        "reserved": 0,
        "resources": [],
        "counts": {},
    }
    plans = report["resources"]
    store.save_report(report)
    wanted = set(request["resource_ids"]) if "resource_ids" in request else None

    def save():
        report["counts"] = dict(Counter(p["outcome"] for p in plans))
        report["reasons"] = dict(Counter(p["reason"] for p in plans))
        store.save_report(report)

    def finish():
        report["finished_at"] = iso(clock())
        save()
        store.release()
        return report

    try:
        discovered = {}
        for resource in gcp.discover(check_time):
            check_time()
            if resource["id"] in discovered:
                continue
            if len(discovered) >= policy.max_resources:
                raise ValueError("Discovery bound exceeded; no workload actions permitted")
            discovered[resource["id"]] = resource
            if wanted is not None and resource["id"] not in wanted:
                continue
            entry = state["resources"].setdefault(resource["id"], {"first_seen_at": iso(clock())})
            action, reason = evaluate(resource, copy.deepcopy(entry), policy, clock())
            plans.append(
                {
                    "id": resource["id"],
                    "kind": resource["kind"],
                    "action": action,
                    "reason": reason,
                    "outcome": "planned",
                    "before": resource["snapshot"],
                    "owner": resource["labels"].get("owner", ""),
                }
            )
        # Reconcile accepted operations even if the resource has disappeared or lost its label.
        planned_ids = {p["id"] for p in plans}
        for name, entry in state["resources"].items():
            if (
                entry.get("operation")
                and name not in planned_ids
                and (wanted is None or name in wanted)
            ):
                plans.append(
                    {
                        "id": name,
                        "kind": entry["operation"]["kind"],
                        "action": None,
                        "reason": "operation_reconciliation",
                        "outcome": "planned",
                    }
                )
        report["discovered"] = len(discovered)
        report["status"] = "executing"
        # Save every planned action before any resource API mutation.
        save()
        store.save_state(state)
    except StateError:
        raise
    except Exception as exc:
        report.update(status="failed", error=type(exc).__name__)
        for p in plans:
            p.update(outcome="not_attempted", reason="discovery_incomplete")
        return finish()

    budget_policy = [operator.max_actions_per_window, operator.window_seconds]
    abort = None
    if state["budget_policy"] is None:
        state["budget_policy"] = budget_policy
    elif state["budget_policy"] != budget_policy:
        abort = "budget_policy_changed"
    order = {"scheduler": 0, "cloud_run": 1, "gke": 2, "compute": 3, "legacy_functions": 4}
    plans.sort(key=lambda p: (order[p["kind"]], p["id"]))
    for plan in plans:
        if abort:
            plan.update(outcome="not_attempted", reason=abort)
            continue
        name = plan["id"]
        entry = state["resources"][name]
        try:
            check_time()
            # Historical operations cannot expand a replacement operator policy.
            if plan["kind"] not in policy.services:
                plan.update(outcome="skipped", reason="service_no_longer_enabled")
                save()
                continue
            policy.validate_name(name, plan["kind"])
            if entry.get("operation"):
                outcome = gcp.poll(entry["operation"])
                if outcome == "pending":
                    plan.update(outcome="skipped", reason="operation_in_progress")
                    save()
                    continue
                submitted = entry.pop("submitted")
                entry.pop("operation")
                if outcome == "failed":
                    entry["blocked"] = {"reason": "operation_failed", **submitted}
                    plan.update(outcome="failed", reason="operation_failed")
                    save()
                    store.save_state(state)
                    continue
                plan["confirmed_action"] = submitted["action"]
                if submitted["action"] == "stop":
                    entry["stop"] = {
                        "fingerprint": submitted["fingerprint"],
                        "submitted_at": submitted["at"],
                    }
                elif submitted["action"] == "delete":
                    entry["deleted"] = submitted
                else:
                    entry["quarantine"] = submitted
                # Do not lose successful reconciliation if later refresh fails.
                save()
                store.save_state(state)
            if entry.get("deleted"):
                plan.update(outcome="skipped", reason="deletion_confirmed")
                save()
                continue
            fresh = gcp.refresh(plan["kind"], name)
            action, reason = evaluate(fresh, entry, policy, clock())
            plan.update(action=action, reason=reason)
            if not action:
                plan["outcome"] = "blocked" if reason == "unresolved_action" else "skipped"
                save()
                store.save_state(state)
                continue
            protection = gcp.protection(fresh, action)
            if protection:
                plan.update(outcome="skipped", reason=protection)
                save()
                continue
            state["reservations"] = [
                r
                for r in state["reservations"]
                if (clock() - timestamp(r["at"])).total_seconds() < operator.window_seconds
            ]
            if report["reserved"] >= policy.max_actions_per_run:
                plan.update(outcome="skipped", reason="run_budget_exhausted")
                continue
            if len(state["reservations"]) >= operator.max_actions_per_window:
                plan.update(outcome="skipped", reason="shared_budget_exhausted")
                continue
            if policy.dry_run:
                report["reserved"] += 1
                plan.update(outcome="would_act", reason=reason)
                save()
                store.save_state(state)
                continue
            latest = gcp.refresh(plan["kind"], name)
            if fingerprint(latest) != fingerprint(fresh):
                plan.update(outcome="skipped", reason="changed_before_action")
                save()
                continue
            protection = gcp.protection(latest, action)
            if protection:
                plan.update(outcome="skipped", reason=protection)
                save()
                continue
            check_time()
            store.assert_owned()
            token = str(uuid.uuid5(uuid.UUID(hex=run_id), name + action))
            intent = {
                "run_id": run_id,
                "action": action,
                "at": iso(clock()),
                "request_id": token,
                "fingerprint": fingerprint(latest, stop_authority=action == "stop"),
                "before": latest["snapshot"],
            }
            entry["pending"] = intent
            state["reservations"].append({"id": name, "at": intent["at"], "request_id": token})
            # A single conditional object replacement commits both intent and budget.
            store.save_state(state)
            plan.update(outcome="pending", intent=intent, before=latest["snapshot"])
            save()
            report["reserved"] += 1
            try:
                operation = gcp.act(latest, action, token)
                if operation:
                    entry.update(operation=operation, submitted=intent)
                else:
                    entry["quarantine"] = intent
                entry.pop("pending")
                plan.update(outcome="submitted", reason="api_accepted", operation=operation)
            except Exception as exc:
                status = exc.status if isinstance(exc, APIError) else None
                if status in RESOURCE_REJECTIONS | {401, 403}:
                    entry.pop("pending")
                    plan.update(outcome="failed", reason=f"api_{status}")
                    if status in (401, 403):
                        abort = "authorization_failure"
                else:
                    plan.update(outcome="unknown", reason=type(exc).__name__)
                    abort = "uncertain_mutation"
            # Outcome first: a failed state write leaves the prior durable pending intent.
            save()
            store.save_state(state)
        except StateError:
            raise  # Do not release the lock after any uncertain checkpoint.
        except Exception as exc:
            status = exc.status if isinstance(exc, APIError) else None
            plan.update(outcome="failed", reason=f"api_{status}" if status else type(exc).__name__)
            if status not in RESOURCE_REJECTIONS:
                abort = "read_or_deadline_failure"
            save()
    report["status"] = (
        "failed"
        if abort
        else "partial"
        if any(p["outcome"] in ("failed", "unknown", "blocked") for p in plans)
        else "complete"
    )
    if abort:
        report["error"] = abort
    return finish()
