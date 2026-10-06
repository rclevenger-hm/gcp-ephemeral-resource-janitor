# Related projects and adopted design patterns

Reviewed against upstream repositories and current Google documentation in October
2026. This service uses independently authored adapters and orchestration. It does
not embed the cleanup engines below. The optional Kubernetes component uses the
official descheduler chart with an authored values file.

| Project | Useful precedent | Applied here |
| --- | --- | --- |
| [ekristen/gcp-nuke](https://github.com/ekristen/gcp-nuke) | ADC, explicit projects/regions, required blocklists, filter presets and dry-run-first destructive cleanup | Explicit bounded project scope, preview decisions and exclusions. Whole-project destructive cleanup is outside this janitor's scope. |
| [Cloud Custodian GCP](https://github.com/cloud-custodian/cloud-custodian/tree/main/tools/c7n_gcp) | Resource/filter/action separation, periodic and event-driven execution, metrics and policy vocabulary | Separate enrollment/eligibility policy and service actions, durable decision reports, structured logs. Deployment follows current Cloud Run documentation rather than historical plugin setup assumptions. |
| [Kubernetes Descheduler](https://github.com/kubernetes-sigs/descheduler) | Established eviction strategies, node-fit checks, PDB integration, protected pod classes, policy limits | Optional separately installed, chart-pinned GKE configuration with dry run, selected pods and small caps. |
| [AWS sibling service](https://github.com/rclevenger-hm/aws-ephemeral-resource-janitor) | Operator-controlled limits, journaled action intent, lifecycle history, overlap protection and service-aware quarantine | Same safety principles, translated to GCS generation preconditions and GCP-specific lifecycle operations. |

The strongest distinction is reversible quarantine with recovery history. Broad
cleanup engines are useful for destroying known disposable environments; this
service keeps a durable account of why it paused a resource and what must be true
before any optional VM deletion. No source code was copied from gcp-nuke or Cloud
Custodian. Consult each upstream license before future code reuse.

## GCP API decisions and sources

- [Run Jobs on a schedule](https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule):
  Cloud Scheduler invokes a Cloud Run Job using OAuth. The job provides a bounded
  batch runtime without maintaining an HTTP server or Kubernetes controller.
- [Cloud Run manual scaling](https://docs.cloud.google.com/run/docs/configuring/services/manual-scaling)
  and [services.patch](https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.services/patch):
  manual count zero disables a service; ETag plus a field mask constrains the update.
- [Manage functions](https://docs.cloud.google.com/functions/docs/managing):
  current functions created with the Functions v2 API can also be managed through
  the Cloud Run Admin API. No irreversible detach is necessary for this design.
- [GKE resize](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/resizing-a-cluster)
  and [nodePools.setSize](https://docs.cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools/setSize):
  use node-pool resizing, avoid concurrent autoscaling, retain per-zone sizes, and
  recognize the documented one-hour limit on PDB/termination-grace handling.
- [NodeConfig](https://docs.cloud.google.com/kubernetes-engine/docs/reference/rest/v1/NodeConfig):
  GCP node resource labels and Kubernetes node labels are distinct.
- [Scheduler pause](https://docs.cloud.google.com/scheduler/docs/reference/rest/v1/projects.locations.jobs/pause)
  and [Job](https://docs.cloud.google.com/scheduler/docs/reference/rest/v1/projects.locations.jobs):
  exact enrollment is needed because Scheduler jobs do not have resource labels.
- [VM stop](https://docs.cloud.google.com/compute/docs/reference/rest/v1/instances/stop)
  and [listReferrers](https://docs.cloud.google.com/compute/docs/reference/rest/v1/instances/listReferrers):
  stable request IDs support deduplication; group referrers provide a protection
  beyond resource-name/label conventions.
- [Cloud Storage preconditions](https://docs.cloud.google.com/storage/docs/request-preconditions):
  generation-conditional creation/replacement/deletion guards lock ownership,
  atomic intent/budget checkpoints and report updates. This does not make GCP
  workload APIs participate in a storage transaction.

Tests validate request shapes against official discovery schemas bundled with the
Google API Python client and intercept real Google Storage SDK HTTP requests. They
exercise local failure/concurrency semantics without contacting real workloads.
Live staging remains necessary to verify a target project's IAM, policies, quotas
and workload-specific lifecycle behavior before production enablement.
