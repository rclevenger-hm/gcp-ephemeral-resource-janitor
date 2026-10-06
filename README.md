# GCP Ephemeral Resource Janitor

A scheduled, opt-in lifecycle service for temporary GCP workloads. Cloud Scheduler
starts a Cloud Run Job; the job discovers resources, persists a plan, enforces a
shared action budget, and checkpoints every attempted action in Cloud Storage.

**Starts in dry run with the timer paused.** No project or organization is selected
implicitly. Cloud credentials come from Application Default Credentials (ADC), not
service-account keys committed to this repository.

| Resource | Supported action | Enrollment and protections |
| --- | --- | --- |
| Compute Engine VM | Stop; optionally delete after a confirmed janitor stop and recovery grace | Explicit labels; skips group members, managed instances, Spot/preemptible VMs, local SSDs and attached instance schedules. Never disables deletion protection. |
| Cloud Run service / current Cloud Run function | Disable through manual scaling to zero | Labels on the underlying Run service; conditional update using its ETag. Preserves revision, image, IAM and unrelated scaling settings. |
| GKE Standard node pool | Resize to zero | GCP node resource labels; additional disruption opt-in; excludes Autopilot, autoscaling pools and node auto-provisioning. Saves current capacity per zone. |
| Cloud Scheduler job | Pause | Exact job names and expiry settings in the operator policy; Scheduler jobs have no resource labels. The janitor's own timer is protected. |
| First-generation Cloud Function | Inventory and explain why it was skipped | No supported reversible pause equivalent; no automatic deletion. |
| Kubernetes descheduler | Optional separate Helm installation | Dry run, selected pods, eviction caps, node-fit and disruption protections. See [DESCHEDULER.md](docs/DESCHEDULER.md). |

This initial release manages one project and explicit regions/zones per deployment.
It does not delete GKE clusters, Cloud Run services/functions, disks independently,
VPCs, databases, Eventarc triggers or whole projects. Stopped/disabled workloads can
retain storage, networking and control-plane costs. Disabling a function can leave
upstream event retries/backlogs; coordinate its producers and retention policy.

## Labels and expiry

Apply these **GCP labels**, not Resource Manager IAM tags. GKE uses
`nodePool.config.resourceLabels`, not Kubernetes node/pod labels.

| Label | Meaning |
| --- | --- |
| `janitor-managed=true` | Required opt-in |
| `janitor-expires-at=2026-12-01t180000z` | Absolute UTC expiry; compact time is valid in GCP label values |
| `janitor-ttl-hours=24` | Positive, finite hours from first observation; used when absolute expiry is absent |
| `do-not-cleanup` (any value) | Exclusion, including when its value is `false` |
| `janitor-allow-delete=true` | Additional VM deletion opt-in; deployment must also use `mode=lifecycle` |
| `janitor-allow-disruption=true` | Additional GKE node-pool resize opt-in |
| `owner=platform` | Optional ownership label copied into reports |

Expiry defaults to 24 hours after first observation when no expiry label is set.
Dry runs establish first-seen times. Invalid or excessive TTLs produce a skipped
resource decision; `NaN`, infinity and overflow do not interrupt other resources.
Absolute expiry takes precedence over TTL. To extend a VM's life, move expiry into
the future; a subsequent observation invalidates its previous deletion authority.

Cloud Scheduler enrollment uses normal ISO timestamps in `scheduler_jobs`; see
[examples/policy.json](examples/policy.json). Do not enroll important schedules by
name pattern. For functions created with the Cloud Functions v2 API, enroll and
manage the underlying Cloud Run service; no irreversible detach is performed.

## Safety model

- Operator policy is loaded only from `JANITOR_POLICY`. CLI requests can enable
  dry run, lower the per-run cap, or select a subset of resource IDs. They cannot
  change project, regions, services, exclusions, lifecycle mode, grace or budgets.
- A generation-checked, project-wide lock serializes all workers using the same
  state bucket. It has **no automatic expiration**. A crashed worker can require
  deliberate recovery; a new worker cannot silently take over a still-running one.
- The complete plan is saved before workload mutations. One conditional state
  write reserves both action intent and rolling budget. Outcomes are checkpointed
  individually. Unknown results remain blocked until reconciled.
- Accepted long-running operations are polled on later invocations. API acceptance
  is not reported as confirmed completion. A VM must have a successful janitor
  stop operation, be observed stopped, remain unchanged and pass recovery grace
  (24 hours by default) before it can be deleted.
- Eligibility is refreshed before action. Cloud Run uses ETag preconditions;
  Compute/GKE/Scheduler APIs do not offer equivalent atomic eligibility guards.
  Compute mutations include a stable request UUID. API mutations are not retried
  automatically. [Residual races and recovery](docs/OPERATIONS.md) are explicit.
- Per-run and rolling-window caps default to 10 actions. One pool resize counts
  as **one action**, regardless of its number of nodes. Failed/unknown submitted
  attempts consume budget too. Every deployment managing a project must share the
  same bucket and budget settings; separate buckets do not coordinate.

## Deploy

Use Python 3.12+, Terraform 1.9+ and a current Google Cloud CLI. The Terraform
configuration creates the job, paused timer, dedicated identities, conditional
state-bucket access, versioned state storage and a Cloud Monitoring error alert.
It does not create your workloads. Review [OPERATIONS.md](docs/OPERATIONS.md) first.

1. Choose a dedicated test project. Enable billing and bootstrap an Artifact
   Registry Docker repository and a **separate durable Terraform state bucket**.
   Use an authorized operator identity for API enablement, IAM, infrastructure
   provisioning and image build/push. Do not give the runtime deployment powers.
2. Build and push the container to your registry. For example, with your normal
   Cloud Build identity and build permissions:

   ```sh
   gcloud builds submit --project YOUR_PROJECT \
     --tag us-central1-docker.pkg.dev/YOUR_PROJECT/janitor/runner:reviewed .
   gcloud artifacts docker images describe \
     us-central1-docker.pkg.dev/YOUR_PROJECT/janitor/runner:reviewed \
     --project YOUR_PROJECT --format='value(image_summary.digest)'
   ```

3. Copy `infra/terraform.tfvars.example` to a local, ignored `.tfvars` file. Set
   your project, bucket and image **by the returned immutable digest**. Keep
   `dry_run=true` and `scheduler_paused=true`. Select only needed services and
   supply existing Monitoring notification-channel names for delivered alerts.

   ```sh
   terraform -chdir=infra init \
     -backend-config=bucket=YOUR_TERRAFORM_STATE_BUCKET \
     -backend-config=prefix=ephemeral-janitor
   terraform -chdir=infra plan -var-file=terraform.tfvars -out=reviewed.tfplan
   terraform -chdir=infra apply reviewed.tfplan
   gcloud run jobs execute ephemeral-janitor \
     --project YOUR_PROJECT --region us-central1 --wait
   ```

4. Read the logs and the report URI. Verify eligibility, skipped reasons and
   before-action snapshots with disposable resources. Set `scheduler_paused=false`
   through Terraform after validating dry runs. Live mode requires a reviewed
   Terraform change to `dry_run=false`; it also grants the selected mutation IAM
   permissions. Start with one action per run/window. Cloud Run live management
   requires explicit `cloud_run_service_accounts`; image access can be granted on
   exact `cloud_run_image_repositories` if needed. Never use project-wide actAs.
5. Enable VM deletion separately with `mode=lifecycle` and the deletion label only
   after stop/recovery has been exercised. Existing disk `autoDelete` settings
   govern which attached disks disappear with a VM; reports capture those settings.

The runtime role has discovery/read permissions and state writes in dry run, but
no workload mutation permissions. GCP IAM bounds the project and API actions;
labels are application-level enrollment, **not IAM enforcement**. GKE's
`container.clusters.update` and Cloud Run's `run.services.update` permissions cover
more operations than the application uses. Treat the runtime identity, image,
policy, state bucket and their writers as privileged trust boundaries.

## Local use and development

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
ruff check .
ruff format --check .
pytest -q
# Requires ADC, a provisioned state bucket, and explicit project access:
export JANITOR_POLICY="$(cat examples/policy.json)"
gcp-janitor --dry-run --max-actions 1
gcp-janitor --report RUN_ID
```

A local dry run writes audit/history state but does not change workloads. Use a
separate test bucket/policy when developing. CI tests use mocked Google HTTP
endpoints and generation-checked storage, plus Terraform mocked-provider tests,
container build and Helm rendering. CI never deploys or calls live GCP APIs.

[Operations and recovery](docs/OPERATIONS.md) ·
[GitHub research and adopted patterns](docs/RESEARCH.md) ·
[Optional descheduler](docs/DESCHEDULER.md)
