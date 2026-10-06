# Operations and recovery

## Before enabling live actions

Use a dedicated sandbox project, an immutable reviewed image, and at least one
successful dry run. Compare discovered IDs, decisions, expiry, disk auto-delete
settings and GKE capacity against the console. Dry run exercises reads and state
writes; it cannot prove that future mutation permissions or service quotas work.
Exercise a single disposable resource of each enabled type before increasing caps.

Keep the Terraform GCS backend in a separate pre-existing bucket, with versioning
and restricted operator access. Preserve `.terraform.lock.hcl`. Never run an
unattended `terraform apply` from an ephemeral local state file. Infrastructure CI
validates without GCP credentials and does not apply plans. For an existing
janitor state bucket, import it into Terraform; do not create a second bucket and
assume its budgets coordinate with the original.

Cloud Build and Artifact Registry bootstrap, the build identity's image push
permissions, deployment operator rights, and existing notification channels are
operator prerequisites. The Cloud Run service agent needs access to the runner
image; same-project Artifact Registry deployments normally have the standard
service-agent grant. For a cross-project image, grant repository access explicitly.
Do not grant service-agent roles to the runtime or scheduler identity. The Cloud
Scheduler service agent must retain its standard service-agent role so it can mint
OAuth credentials for the timer's invoker identity.

Only trusted operators can edit the job's environment, deploy its image or grant
`run.jobs.runWithOverrides`. The timer gets only `roles/run.invoker` on the one job;
it cannot override environment variables. The Python request interface accepts
only a subset selection, a lower action cap and dry run. GCP job configuration
changes are privileged deployment operations, outside this request boundary.

## Reports and monitoring

Every run has a UUID-hex ID. In Cloud Run it is derived from `CLOUD_RUN_EXECUTION`,
so retried tasks within one execution use the same ID. Duplicate Scheduler
requests can create **different executions**. The shared lock, operation history
and rolling budget control these overlaps; this is not exactly-once delivery or
schedule-slot deduplication. Cloud Run has one task, no automatic task retries,
and a ten-minute limit. The application stops starting work with 90 seconds left
in its eight-minute work deadline, leaving time to checkpoint.

Structured stdout includes `severity`, `event`, status, counts, reasons and
`report_uri`. A successful Scheduler dispatch only means the Run API accepted an
execution. Inspect the Run execution and report for the outcome. Terraform creates
a log-based Monitoring alert for job errors and failed Scheduler dispatches.
Without supplied notification channels it creates incidents but delivers no
notifications. Monitor execution cadence externally as well: an intentionally
paused/deleted timer or a silent failure to start cannot emit an application log.

Objects are stored under `v1/projects/PROJECT_NUMBER/`:

| Object | Purpose |
| --- | --- |
| `lock.json` | Exclusive project worker owner and acquisition time |
| `state.json` | First observations, accepted operations, unresolved intents, lifecycle history, rolling reservations and budget policy |
| `runs/RUN_ID.json` | Complete plan, before-action snapshots and individual outcomes |

Reports include skipped reasons and original capacity/scaling/disk settings.
`submitted` means an API accepted an asynchronous operation; it does **not** prove
the resource reached its target. A later run records `confirmed_action`, or blocks
the resource on a failed operation. Scheduler pause is acknowledged synchronously.
An operation still running consumes no additional action budget on subsequent
polls. Budget is a rolling time window across all regions using this state bucket,
not a resettable per-Lambda-style invocation counter.

Expected resource rejection (400, 404, 409, 412 or 422) records a failure and allows
unrelated candidates to continue. Authorization failure stops the run. Timeouts,
429/5xx and malformed mutation acknowledgements are uncertain: the intent remains
pending, later actions stop, and subsequent runs refuse that resource. A state or
report checkpoint failure retains the project lock. Reads and discovery must
succeed before new workload actions; an incomplete discovery stops the run.

Run reports expire after 90 days in the supplied bucket lifecycle policy. Current
state, locks and history do not expire. Versioning preserves overwritten state
and reports; noncurrent state versions can accumulate storage costs. The design
uses one atomic state object per project, appropriate for bounded ephemeral
inventories (1,000 discovered candidates per run by default). It is not a
high-throughput organization inventory. Historical resource records accumulate;
archive them through a reviewed maintenance procedure with the timer paused,
workers stopped, backups retained, and generation-conditional writes. Never remove
pending operations or active reservations to save space.

## Retained lock / uncertain mutation recovery

A lock never expires automatically. GCS generation checks fence storage writes,
but no GCS lease can fence an old worker paused immediately before a GCP API call.
Do not delete a lock merely because its timestamp looks old.

1. Pause the timer and prevent new executions. Cancel active Run executions; verify
   they are terminal and that no local/manual worker remains. If uncertain, revoke
   the old runtime's mutation permissions and wait for propagation. Establish that
   the previous worker cannot issue another API call before proceeding.
2. Copy the lock, current state and affected report **with their generations** to
   a restricted incident location. Inspect accepted operation IDs and the target
   resources. Check Cloud Audit Logs, request UUIDs where supported, tags/state,
   actual capacity and original report snapshots. Do not blindly resubmit a request
   with an unknown result. Operation records can age out of GCP; a missing operation
   is not evidence that the action never happened.
3. Reconcile each unresolved `pending` or `blocked` entry. If completion is proven,
   record a reviewed history entry compatible with the schema, retaining the
   original action time, fingerprint and snapshot. If the effect cannot be proven,
   keep the resource excluded and blocked. If a request is proven not to have
   taken effect, clear its pending entry only after documenting that evidence.
   Preserve charged reservations until their rolling window expires.
4. Checkpoint the reviewed `state.json` with `if_generation_match=GENERATION_READ`.
   A precondition failure means the object changed: stop and investigate. Do not
   use an unconditional overwrite, delete/recreate, `force`, or a new state bucket.
5. Delete only the inspected lock generation with a precondition. Review a dry run,
   restore necessary IAM if revoked, then resume the timer through Terraform.

Use the Cloud Storage SDK `Blob.upload_from_string(..., if_generation_match=N,
retry=None)` and `Blob.delete(if_generation_match=N, retry=None)` for these
operator-reviewed changes. There is deliberately no force-unlock shortcut. Storage
writers are privileged and must not edit live state concurrently with workers.

Budget policy changes (window or shared cap) deliberately stop actions until
reconciled. Pause all workers, preserve reservations, review the change, and
conditionally update `budget_policy` to `[NEW_SHARED_CAP, NEW_WINDOW_SECONDS]`.
Extending the window after older reservations were pruned needs a full wait for
the new window without mutations, or reconstruction from reports/audit logs.
Changing only a per-run cap needs no state migration. Never reset budgets to
bypass a cap. Keep the same state bucket when changing regions or deployments.

## Restore a workload

Restoration is a deliberate operator action; the runtime has no resume/start
permissions. First move expiry into the future or exclude the resource, then
observe that exclusion/extension before restoring. Infrastructure-as-code or other
controllers must agree with the desired restored state.

| Resource | Restore approach |
| --- | --- |
| Compute VM | If still present, `gcloud compute instances start NAME --project PROJECT --zone ZONE`. Deleted VMs and auto-deleted disks are not recoverable through this service; use your snapshots/backups. |
| Cloud Run / current function | Use `gcloud run services update NAME --project PROJECT --region REGION --scaling=auto` if the snapshot had automatic scaling, or its prior manual count. Preserve prior min/max fields. |
| GKE pool | Use `gcloud container clusters resize CLUSTER --node-pool POOL --location LOCATION --project PROJECT --num-nodes N`. N is **per zone**, not the sum of `counts_per_zone`. If counts differed across zones, investigate before restoring; do not set N to the total. |
| Scheduler | `gcloud scheduler jobs resume NAME --project PROJECT --location REGION`; coordinate upstream retries and any catch-up behavior. |

For paused non-VM resources, a durable quarantine marker prevents automatically
pausing a manually restored resource again while the same expiry remains expired.
After expiry is extended and observed, the marker is cleared for the next lifecycle.
For VMs, restarting changes the observed fingerprint and invalidates deletion
history, but an **expired** running VM can be stopped again: extend/exclude first.
A replacement resource at a previously deleted VM path stays blocked by deletion
history until an operator reviews and clears that old history. This favors avoiding
an accidental new-resource deletion over automatic name reuse.

## Service-specific boundaries

**Compute:** STOP means GCE status `TERMINATED`, not resource deletion. Only VMs
stopped by a successfully reconciled janitor operation can become delete candidates.
Grace starts when a stopped VM is observed after confirmation, so API latency does
not shorten recovery time. Label/state/disk/identity changes invalidate or reset
history. Attached persistent disks retain their existing `autoDelete` policy; a VM
delete may delete those disks. No Local SSD discard or deletion-protection override
is sent. Instance-group referrers and managed-node metadata are checked separately
from labels, so GKE/MIG nodes are not managed as standalone VMs.

**Cloud Run:** updates use the current ETag and a scaling-only field mask. Modern
Cloud Run functions, including functions managed through the Functions v2 API,
use the underlying Run service without detaching it. Apply enrollment labels to
that service, not only a different Functions metadata object. HTTP requests to a
disabled service fail; event sources may retry. This does not cancel Cloud Run Job
executions or manage Run worker pools. Pausing a Scheduler trigger prevents future
scheduled invocations, not jobs already running or calls from other producers.

**GKE:** resize only explicitly labeled Standard node pools without autoscaling or
node auto-provisioning. An extra disruption label is required. The report captures
current managed-group target sizes, never the creation-time `initialNodeCount`.
GKE drains removed nodes and respects PDB/termination grace for **up to one hour**;
that is not an unlimited disruption guarantee. A pool can have many nodes while
consuming one API action. Keep essential/system capacity and the descheduler on a
pool that is not enrolled. Resizing a pool does not suspend Kubernetes Deployments,
CronJobs or controllers, and remaining node pools can still run their pods.
Autopilot workload-level lifecycle needs a separate Kubernetes-aware integration.

**Races and limits:** snapshots are re-read and Cloud Run uses an ETag. Compute,
GKE and Scheduler mutations lack an atomic precondition for all eligibility
labels/state. Their state or group membership can change between check and call;
no application lock excludes human/other-controller changes. Tag changes reverted
between observations cannot be detected reliably. Isolate ephemeral resources,
coordinate controllers, constrain runtime IAM and avoid concurrently changing
managed resources. No exactly-once mutation or zero-disruption guarantee is made.
