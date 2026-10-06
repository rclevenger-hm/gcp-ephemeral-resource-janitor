"""Cloud Run Job entry point and local CLI. Operator policy is the only authority."""

import argparse
import json
import os
import uuid

from .config import load_policy
from .engine import run
from .gcp import GCP


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resource-id", action="append")
    parser.add_argument("--max-actions", type=int)
    parser.add_argument("--report", help="Read a durable report by its UUID hex run ID")
    args = parser.parse_args()
    try:
        policy = load_policy()
        gcp = GCP(policy)
        store = gcp.store()
        if args.report:
            report_id = uuid.UUID(hex=args.report).hex
            gcp.verify_project()
            report, _ = store.read(f"runs/{report_id}.json")
            if report is None:
                raise ValueError("Report not found")
            print(json.dumps(report, indent=2))
            return 0
        request = {}
        if args.dry_run:
            request["dry_run"] = True
        if args.resource_id is not None:
            request["resource_ids"] = args.resource_id
        if args.max_actions is not None:
            request["max_actions_per_run"] = args.max_actions
        execution = os.environ.get("CLOUD_RUN_EXECUTION")
        run_id = uuid.uuid5(uuid.NAMESPACE_URL, execution).hex if execution else None
        report = run(policy, request, gcp, store, run_id=run_id)
        summary = {k: v for k, v in report.items() if k not in ("resources", "policy")}
        summary.update(
            severity="INFO" if report["status"] == "complete" else "ERROR",
            event="janitor_run",
            report_uri=f"gs://{policy.state_bucket}/{store.prefix}runs/{report['run_id']}.json",
        )
        print(json.dumps(summary, sort_keys=True))
        return 0 if report["status"] == "complete" else 1
    except Exception as exc:
        print(
            json.dumps(
                {"severity": "ERROR", "event": "janitor_failure", "error": type(exc).__name__}
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
