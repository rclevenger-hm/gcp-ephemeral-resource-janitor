import json
import re
from urllib.parse import parse_qs, urlparse

import pytest
import responses
from google.api_core.exceptions import PreconditionFailed
from google.auth.credentials import AnonymousCredentials
from google.cloud import storage

from janitor.state import GCSObjects, StateError


@pytest.fixture
def objects(monkeypatch):
    # SDK observability can fetch bucket metadata on a background thread. Disable
    # that enrichment so requests cannot race with or outlive a test's HTTP mocks.
    # Explicit bucket ownership verification still performs its normal GET.
    monkeypatch.setenv("DISABLE_GCS_PYTHON_CLIENT_OTEL_BUCKET_METADATA", "true")
    client = storage.Client(project="janitor-test-123", credentials=AnonymousCredentials())
    return GCSObjects(client.bucket("janitor-state"))


@responses.activate
def test_real_sdk_upload_uses_generation_guard_and_no_retries(objects):
    url = re.compile(r"https://storage.googleapis.com/upload/storage/v1/b/janitor-state/o.*")
    responses.post(url, json={"name": "state.json", "generation": "12"})
    assert objects.write("state.json", {"hello": "world"}, 11) == 12
    upload = responses.calls[-1].request
    query = parse_qs(urlparse(upload.url).query)
    assert query["ifGenerationMatch"] == ["11"]
    assert b'"hello": "world"' in upload.body
    responses.post(url, status=412, json={"error": {"code": 412, "message": "conflict"}})
    with pytest.raises(PreconditionFailed):
        objects.write("state.json", {}, 0)
    assert len([c for c in responses.calls if c.request.method == "POST"]) == 2


@responses.activate
def test_real_sdk_metadata_and_download_share_exact_generation(objects):
    path = "https://storage.googleapis.com/storage/v1/b/janitor-state/o/state.json"
    responses.get(path, json={"name": "state.json", "generation": "7", "size": "2"})
    responses.get(
        re.compile(r"https://storage.googleapis.com/download/storage/v1/b/.*"),
        body=json.dumps({"schema": 1}),
        content_type="application/json",
    )
    body, generation = objects.read("state.json")
    assert body == {"schema": 1} and generation == 7
    query = parse_qs(urlparse(responses.calls[-1].request.url).query)
    assert query["ifGenerationMatch"] == ["7"]
    assert query["generation"] == ["7"]


@pytest.mark.parametrize("status", [204, 412])
@responses.activate
def test_real_sdk_delete_is_generation_guarded(objects, status):
    # An unrelated read must not change which request the assertion checks.
    responses.get(
        "https://storage.googleapis.com/storage/v1/b/janitor-state",
        json={"name": "janitor-state", "projectNumber": "123456789012"},
    )
    objects.verify_project("123456789012")
    url = "https://storage.googleapis.com/storage/v1/b/janitor-state/o/lock.json"
    responses.delete(
        url,
        status=status,
        match=[
            responses.matchers.query_param_matcher({"ifGenerationMatch": "9"}, strict_match=False)
        ],
    )
    if status == 412:
        with pytest.raises(PreconditionFailed):
            objects.delete("lock.json", 9)
    else:
        objects.delete("lock.json", 9)
    deletes = [c.request for c in responses.calls if c.request.method == "DELETE"]
    assert len(deletes) == 1  # A precondition rejection must not be retried.
    assert urlparse(deletes[0].url)._replace(query="").geturl() == url
    assert parse_qs(urlparse(deletes[0].url).query)["ifGenerationMatch"] == ["9"]


@responses.activate
def test_bucket_must_belong_to_configured_project(objects):
    responses.get(
        "https://storage.googleapis.com/storage/v1/b/janitor-state",
        json={"name": "janitor-state", "projectNumber": "99"},
    )
    with pytest.raises(StateError):
        objects.verify_project("123456789012")
