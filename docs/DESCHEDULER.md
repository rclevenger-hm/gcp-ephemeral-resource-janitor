# Optional Kubernetes descheduler for GKE

The GCP janitor pauses cloud resources. Kubernetes Descheduler evicts selected pods
so the Kubernetes scheduler can place replacements on suitable nodes. It does not
suspend CronJobs, remove Deployments/clusters, or guarantee fewer billable nodes.
Install it separately with a Kubernetes operator identity; the Cloud Run Job has no
Kubernetes credentials, cluster-admin binding or private cluster endpoint access.

`deploy/descheduler-values.yaml` is an authored configuration for the official
upstream Helm chart **0.34.0**. It starts in dry run, runs every 15 minutes, forbids
overlapping CronJobs, and caps evictions at one per node, two per namespace and
three total. Only pods labeled `janitor-managed=true` qualify. Node-fit,
minimum-replica and minimum-age checks apply. PVC pods, pods without PDBs,
resource-claim pods and the upstream default protected categories remain protected.
System namespaces are excluded and prefer-no-eviction annotations are mandatory.

Only node-affinity and node-taint violation strategies are enabled. These move
misplaced pods; they are not a TTL deletion or scale-to-zero workflow. PDB eviction
checks do not guarantee zero disruption. GKE node-pool resizing has its own drain
behavior and a one-hour limit on respecting disruption/termination grace. Installing
this descheduler does not extend that limit or drain every pod before a pool resize.

Check the chart's [release-specific compatibility guidance](https://github.com/kubernetes-sigs/descheduler/tree/release-1.34#compatibility)
against your GKE cluster version. Review schemas and rendered RBAC when changing
chart versions; do not use current master policy examples with an older chart.

```sh
helm repo add descheduler https://kubernetes-sigs.github.io/descheduler/
helm repo update
helm template ephemeral-descheduler descheduler/descheduler \
  --version 0.34.0 --namespace kube-system \
  --values deploy/descheduler-values.yaml > /tmp/descheduler-rendered.yaml
# Review policy, CronJob and RBAC before installing.
helm upgrade --install ephemeral-descheduler descheduler/descheduler \
  --version 0.34.0 --namespace kube-system \
  --values deploy/descheduler-values.yaml
```

Review proposed evictions and establish PDBs. Deliberately change
`cmdOptions.dry-run` to `false` only after reviewing the workload impact, then
upgrade the Helm release. Place the descheduler on persistent/system capacity
outside enrolled node pools. To stop it, set the chart's `suspend=true` or uninstall
its release. This repository does not install it into a cluster automatically.
