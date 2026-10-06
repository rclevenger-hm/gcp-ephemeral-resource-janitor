import copy
from datetime import datetime, timezone

import pytest
from google.api_core.exceptions import PreconditionFailed

from janitor.config import Policy
from janitor.gcp import GCP
from janitor.state import Store

NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
PROJECT = "janitor-test-123"
PREFIX = f"projects/{PROJECT}"


class Objects:
    """A generation-checked object store, shared by independent workers."""

    def __init__(self):
        self.data = {}
        self.generation = 0
        self.fail = lambda key, body: False
        self.writes = []

    def read(self, key):
        return copy.deepcopy(self.data.get(key, (None, 0)))

    def write(self, key, body, generation):
        if self.fail(key, body):
            raise OSError("lost acknowledgement")
        if self.data.get(key, (None, 0))[1] != generation:
            raise PreconditionFailed("stale generation")
        self.generation += 1
        self.data[key] = copy.deepcopy(body), self.generation
        self.writes.append((key, copy.deepcopy(body)))
        return self.generation

    def delete(self, key, generation):
        if self.data[key][1] != generation:
            raise PreconditionFailed("stale generation")
        del self.data[key]


@pytest.fixture
def policy():
    return Policy(
        project_id=PROJECT,
        project_number="123456789012",
        state_bucket="janitor-state",
        regions=("us-central1",),
        zones=("us-central1-a",),
        dry_run=False,
    )


@pytest.fixture
def objects():
    return Objects()


@pytest.fixture
def store(objects, policy):
    return Store(objects, policy)


def vm(name="vm-a", **kwargs):
    return dict(
        name=name,
        id="123",
        status="RUNNING",
        labels={"janitor-managed": "true", "janitor-expires-at": "2026-10-01t000000z"},
        labelFingerprint="label-v1",
        lastStartTimestamp="2026-09-01T00:00:00Z",
        disks=[dict(type="PERSISTENT", autoDelete=True, source="disk-a")],
        **kwargs,
    )


class Cloud:
    def __init__(self, policy, raws=None):
        self.policy = policy
        self.adapter = GCP(policy, session=object())
        self.resources = {}
        for raw in raws or [vm()]:
            name = f"{PREFIX}/zones/us-central1-a/instances/{raw['name']}"
            self.resources[name] = self.adapter.normalize("compute", name, raw)
        self.actions = []
        self.failures = {}
        self.poll_result = "succeeded"
        self.refresh_hook = lambda resource: resource
        self.act_hook = lambda resource: None
        self.discovery_error = None
        self.protection_result = None

    def verify_project(self):
        pass

    def discover(self, check_time):
        yield from copy.deepcopy(list(self.resources.values()))
        if self.discovery_error:
            raise self.discovery_error

    def refresh(self, kind, name):
        return self.refresh_hook(copy.deepcopy(self.resources[name]))

    def protection(self, resource, action):
        return self.protection_result

    def act(self, resource, action, token):
        self.act_hook(resource)
        self.actions.append((resource["id"], action, token))
        if resource["id"] in self.failures:
            raise self.failures[resource["id"]]
        return {"kind": resource["kind"], "path": f"{PREFIX}/zones/us-central1-a/operations/op-1"}

    def poll(self, operation):
        return self.poll_result


@pytest.fixture
def cloud(policy):
    return Cloud(policy)
