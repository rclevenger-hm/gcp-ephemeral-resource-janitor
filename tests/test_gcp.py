import json
import uuid
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import responses
from conftest import PREFIX, vm
from googleapiclient.discovery_cache import get_static_doc

from janitor.config import ConfigError
from janitor.gcp import BASES, GCP, APIError


@pytest.fixture
def api(policy):
    return GCP(policy, session=requests.Session())


def schema(api, version):
    return json.loads(get_static_doc(api, version))


@responses.activate
def test_compute_stop_uses_documented_request_id_and_no_force(api):
    name = f"{PREFIX}/zones/us-central1-a/instances/vm-a"
    responses.post(BASES["compute"] + name + "/stop", json={"name": "op-1"})
    resource = api.normalize("compute", name, vm())
    token = str(uuid.uuid4())
    op = api.act(resource, "stop", token)
    request = responses.calls[0].request
    assert parse_qs(urlparse(request.url).query) == {"requestId": [token]}
    assert request.body is None
    method = schema("compute", "v1")["resources"]["instances"]["methods"]["stop"]
    assert method["httpMethod"] == request.method
    assert "requestId" in method["parameters"]
    assert op["path"] == f"{PREFIX}/zones/us-central1-a/operations/op-1"


@responses.activate
def test_compute_delete_retains_existing_disk_autodelete_semantics(api):
    name = f"{PREFIX}/zones/us-central1-a/instances/vm-a"
    responses.delete(BASES["compute"] + name, json={"name": "op-2"})
    api.act(api.normalize("compute", name, vm()), "delete", str(uuid.uuid4()))
    assert set(parse_qs(urlparse(responses.calls[0].request.url).query)) == {"requestId"}


@responses.activate
def test_cloud_run_disable_is_conditional_and_only_changes_scaling(api):
    name = f"{PREFIX}/locations/us-central1/services/service-a"
    responses.patch(
        BASES["cloud_run"] + name, json={"name": f"{PREFIX}/locations/us-central1/operations/op-1"}
    )
    resource = api.normalize(
        "cloud_run",
        name,
        dict(
            name=name,
            uid="unique",
            etag="etag-v1",
            generation="3",
            scaling={"minInstanceCount": 2},
            terminalCondition={"state": "CONDITION_SUCCEEDED"},
        ),
    )
    api.act(resource, "disable_service", "unused")
    request = responses.calls[0].request
    body = json.loads(request.body)
    assert body == {
        "name": name,
        "etag": "etag-v1",
        "scaling": {"scalingMode": "MANUAL", "manualInstanceCount": 0},
    }
    assert parse_qs(urlparse(request.url).query) == {
        "updateMask": ["scaling.scalingMode,scaling.manualInstanceCount"]
    }
    spec = schema("run", "v2")
    fields = spec["schemas"]["GoogleCloudRunV2ServiceScaling"]["properties"]
    assert {"scalingMode", "manualInstanceCount"} <= fields.keys()
    assert "MANUAL" in fields["scalingMode"]["enum"]
    service = spec["schemas"]["GoogleCloudRunV2Service"]["properties"]
    assert "etag" in service


@responses.activate
def test_gke_uses_current_per_zone_target_not_initial_node_count(api):
    name = f"{PREFIX}/locations/us-central1/clusters/cluster-a/nodePools/pool-a"
    group = f"{PREFIX}/zones/us-central1-b/instanceGroupManagers/group-a"
    responses.get(BASES["compute"] + group, json={"targetSize": 3, "currentActions": {"none": 3}})
    raw = {
        "name": "pool-a",
        "status": "RUNNING",
        "initialNodeCount": 99,
        "instanceGroupUrls": ["https://www.googleapis.com/compute/v1/" + group],
        "config": {
            "resourceLabels": {"janitor-managed": "true", "janitor-allow-disruption": "true"}
        },
    }
    resource = api.normalize("gke", name, raw, {"status": "RUNNING"})
    assert resource["snapshot"]["counts_per_zone"] == {group: 3}
    assert resource["protection"] is None
    responses.post(BASES["gke"] + name + ":setSize", json={"name": "operation-1"})
    api.act(resource, "scale_pool_zero", "unused")
    assert json.loads(responses.calls[-1].request.body) == {"nodeCount": 0}
    spec = schema("container", "v1")
    assert "nodeCount" in spec["schemas"]["SetNodePoolSizeRequest"]["properties"]
    assert "resourceLabels" in spec["schemas"]["NodeConfig"]["properties"]


@pytest.mark.parametrize(
    "pool,cluster,reason",
    [
        ({"autoscaling": {"enabled": True}}, {}, "autoscaling_enabled"),
        ({}, {"autopilot": {"enabled": True}}, "autopilot_cluster"),
        ({}, {"autoscaling": {"enableNodeAutoprovisioning": True}}, "autoscaling_enabled"),
        ({}, {}, "disruption_not_approved"),
    ],
)
def test_gke_managed_or_unapproved_pools_are_protected(api, pool, cluster, reason):
    name = f"{PREFIX}/locations/us-central1/clusters/c/nodePools/p"
    result = api.normalize("gke", name, pool, cluster)
    assert result["protection"] == reason


@responses.activate
def test_scheduler_has_explicit_enrollment_and_pause_action(policy):
    name = f"{PREFIX}/locations/us-central1/jobs/ephemeral-job"
    api = GCP(
        replace(
            policy, scheduler_jobs={name: {"owner": "team", "expires_at": "2026-01-01T00:00:00Z"}}
        ),
        session=requests.Session(),
    )
    responses.get(BASES["scheduler"] + name, json={"name": name, "state": "ENABLED"})
    r = api.refresh("scheduler", name)
    assert r["labels"]["owner"] == "team"
    assert r["snapshot"]["expires_at"] == "2026-01-01T00:00:00Z"
    responses.post(BASES["scheduler"] + name + ":pause", json={"name": name, "state": "PAUSED"})
    assert api.act(r, "pause_job", "unused") is None
    assert json.loads(responses.calls[-1].request.body) == {}
    assert "labels" not in schema("cloudscheduler", "v1")["schemas"]["Job"]["properties"]


@responses.activate
def test_discovery_paginates_and_rejects_partial_results(api):
    path = f"{PREFIX}/zones/us-central1-a/instances"
    responses.get(BASES["compute"] + path, json={"items": [vm()], "nextPageToken": "next"})
    responses.get(BASES["compute"] + path, json={"items": [vm("vm-b")]})
    assert len(list(api.pages("compute", path, "items"))) == 2
    assert parse_qs(urlparse(responses.calls[-1].request.url).query) == {"pageToken": ["next"]}
    responses.get(BASES["compute"] + path, json={"unreachable": ["us-central1-a"]})
    with pytest.raises(APIError):
        list(api.pages("compute", path, "items"))


@responses.activate
def test_pagination_cycles_fail_closed(api):
    path = f"{PREFIX}/zones/us-central1-a/instances"
    responses.get(BASES["compute"] + path, json={"nextPageToken": "same"})
    with pytest.raises(APIError):
        list(api.pages("compute", path, "items"))
    assert len(responses.calls) == 2


@responses.activate
def test_vm_group_members_are_protected(api):
    name = f"{PREFIX}/zones/us-central1-a/instances/vm-a"
    responses.get(BASES["compute"] + name + "/referrers", json={"items": [{"referrer": "group"}]})
    assert api.protection(api.normalize("compute", name, vm()), "stop") == "referenced_instance"


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"scheduling": {"preemptible": True}}, "spot_or_preemptible"),
        ({"scheduling": {"provisioningModel": "SPOT"}}, "spot_or_preemptible"),
        ({"disks": [{"type": "SCRATCH"}]}, "local_ssd"),
        ({"resourcePolicies": ["schedule"]}, "instance_schedule_attached"),
        ({"metadata": {"items": [{"key": "created-by", "value": "group"}]}}, "managed_instance"),
    ],
)
def test_compute_safety_exclusions(api, changes, reason):
    raw = vm()
    raw.update(changes)
    r = api.normalize("compute", f"{PREFIX}/zones/us-central1-a/instances/vm-a", raw)
    assert r["protection"] == reason


@responses.activate
def test_deletion_protection_is_checked_without_disabling_it(api):
    name = f"{PREFIX}/zones/us-central1-a/instances/vm-a"
    responses.get(BASES["compute"] + name + "/referrers", json={})
    r = api.normalize("compute", name, vm(deletionProtection=True))
    assert api.protection(r, "delete") == "deletion_protected"


@responses.activate
def test_mutation_timeout_is_not_retried(api):
    name = f"{PREFIX}/zones/us-central1-a/instances/vm-a"
    responses.post(BASES["compute"] + name + "/stop", body=requests.Timeout())
    with pytest.raises(requests.Timeout):
        api.act(api.normalize("compute", name, vm()), "stop", str(uuid.uuid4()))
    assert len(responses.calls) == 1


@responses.activate
def test_project_number_identity_checked_before_work(api):
    responses.get(
        BASES["projects"] + PREFIX,
        json={"name": "projects/999", "projectId": api.policy.project_id, "state": "ACTIVE"},
    )
    with pytest.raises(ConfigError):
        api.verify_project()


@pytest.mark.parametrize(
    "name",
    [
        "projects/other-project/zones/us-central1-a/instances/vm-a",
        f"{PREFIX}/zones/europe-west1-a/instances/vm-a",
        f"{PREFIX}/zones/us-central1-a/instances/../../evil",
        "https://attacker.invalid/",
    ],
)
def test_resource_names_cannot_escape_scope(api, name):
    with pytest.raises(ConfigError):
        api.refresh("compute", name)


def test_legacy_functions_are_inventory_only(api):
    name = f"{PREFIX}/locations/us-central1/functions/function-a"
    r = api.normalize(
        "legacy_functions", name, {"name": name, "labels": {"janitor-managed": "true"}}
    )
    assert r["protection"] == "first_generation_function_has_no_pause"


@pytest.mark.parametrize(
    "kind,body,outcome",
    [
        ("compute", {"status": "DONE"}, "succeeded"),
        ("compute", {"status": "RUNNING"}, "pending"),
        ("gke", {"status": "DONE", "error": {"code": 7}}, "failed"),
        ("cloud_run", {"done": True, "response": {}}, "succeeded"),
        ("cloud_run", {"done": True, "error": {"code": 7}}, "failed"),
    ],
)
@responses.activate
def test_operation_completion_is_explicit(api, kind, body, outcome):
    scope = "zones/us-central1-a" if kind == "compute" else "locations/us-central1"
    path = f"{PREFIX}/{scope}/operations/op-1"
    responses.get(BASES[kind] + path, json=body)
    assert api.poll({"kind": kind, "path": path}) == outcome
