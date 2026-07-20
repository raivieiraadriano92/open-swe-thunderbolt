"""Tests for the Linear webhook Issue-label trigger path."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent.api.app import app
from agent.webhooks import common as webhook_common
from agent.webhooks import linear as linear_webhook
from agent.webhooks import linear_routes

_SECRET = "test-linear-secret"


def _sign(body: bytes) -> str:
    return hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _post_linear(client: TestClient, payload: dict) -> Any:
    body = json.dumps(payload, separators=(",", ":")).encode()
    return client.post(
        "/webhooks/linear",
        content=body,
        headers={"Linear-Signature": _sign(body), "Content-Type": "application/json"},
    )


def _issue_payload(*, action: str = "update", labels: list[str] | None = None) -> dict:
    return {
        "type": "Issue",
        "action": action,
        "data": {
            "id": "issue-abc",
            "title": "Fix the flaky test",
            "labelIds": [f"label-{name}" for name in (labels or [])],
        },
    }


def _full_issue_details(*, labels: list[str]) -> dict:
    return {
        "id": "issue-abc",
        "identifier": "OS-42",
        "title": "Fix the flaky test",
        "description": "Do the thing",
        "url": "https://linear.app/x/issue/OS-42",
        "labels": {"nodes": [{"id": f"l-{n}", "name": n} for n in labels]},
        "creator": {"email": "zhen@example.com", "name": "Zhen"},
        "team": {"name": "Core", "key": "OS"},
        "project": {"name": "Backlog"},
        "comments": {"nodes": []},
    }


@pytest.fixture(autouse=True)
def _webhook_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webhook_common, "LINEAR_WEBHOOK_SECRET", _SECRET)


def test_linear_issue_labeled_openswe_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduled: dict[str, Any] = {}

    async def fake_process(issue: dict, repo_config: dict) -> None:
        scheduled["issue"] = issue
        scheduled["repo_config"] = repo_config

    async def fake_fetch(_id: str) -> dict:
        return _full_issue_details(labels=["openswe", "bug"])

    async def fake_default_repo() -> dict[str, str]:
        return {"owner": "langchain-ai", "name": "open-swe"}

    async def fake_thread_exists(_thread_id: str) -> bool:
        return False

    monkeypatch.setattr(linear_webhook, "process_linear_issue", fake_process)
    monkeypatch.setattr(linear_routes.service, "process_linear_issue", fake_process)
    monkeypatch.setattr(webhook_common, "fetch_linear_issue_details", fake_fetch)
    monkeypatch.setattr(webhook_common, "get_team_default_repo", fake_default_repo)
    monkeypatch.setattr(webhook_common, "_thread_exists", fake_thread_exists)

    client = TestClient(app)
    response = _post_linear(client, _issue_payload(action="update", labels=["openswe"]))

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert scheduled["repo_config"]["name"] == "open-swe"
    assert scheduled["issue"]["id"] == "issue-abc"


def test_linear_issue_without_openswe_label_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_process(_issue: dict, _repo: dict) -> None:
        raise AssertionError("process_linear_issue should not be called")

    async def fake_fetch(_id: str) -> dict:
        return _full_issue_details(labels=["bug"])

    monkeypatch.setattr(linear_routes.service, "process_linear_issue", fake_process)
    monkeypatch.setattr(webhook_common, "fetch_linear_issue_details", fake_fetch)

    client = TestClient(app)
    response = _post_linear(client, _issue_payload(action="update", labels=["bug"]))

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert "does not carry" in response.json()["reason"]


def test_linear_issue_label_skips_when_thread_already_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_process(_issue: dict, _repo: dict) -> None:
        raise AssertionError("process_linear_issue should not be called")

    async def fake_fetch(_id: str) -> dict:
        return _full_issue_details(labels=["openswe"])

    async def fake_thread_exists(_thread_id: str) -> bool:
        return True

    async def fake_default_repo() -> dict[str, str]:
        return {"owner": "langchain-ai", "name": "open-swe"}

    monkeypatch.setattr(linear_routes.service, "process_linear_issue", fake_process)
    monkeypatch.setattr(webhook_common, "fetch_linear_issue_details", fake_fetch)
    monkeypatch.setattr(webhook_common, "_thread_exists", fake_thread_exists)
    monkeypatch.setattr(webhook_common, "get_team_default_repo", fake_default_repo)

    client = TestClient(app)
    response = _post_linear(client, _issue_payload(action="update", labels=["openswe"]))

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert "thread already exists" in response.json()["reason"]


def test_linear_issue_create_with_openswe_label_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled: dict[str, Any] = {}

    async def fake_process(issue: dict, repo_config: dict) -> None:
        scheduled["issue"] = issue
        scheduled["repo_config"] = repo_config

    async def fake_fetch(_id: str) -> dict:
        return _full_issue_details(labels=["openswe"])

    async def fake_default_repo() -> dict[str, str]:
        return {"owner": "langchain-ai", "name": "open-swe"}

    async def fake_thread_exists(_thread_id: str) -> bool:
        return False

    monkeypatch.setattr(linear_routes.service, "process_linear_issue", fake_process)
    monkeypatch.setattr(webhook_common, "fetch_linear_issue_details", fake_fetch)
    monkeypatch.setattr(webhook_common, "_thread_exists", fake_thread_exists)
    monkeypatch.setattr(webhook_common, "get_team_default_repo", fake_default_repo)

    client = TestClient(app)
    response = _post_linear(client, _issue_payload(action="create", labels=["openswe"]))

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert scheduled["issue"]["identifier"] == "OS-42"


def test_linear_non_comment_non_issue_events_still_ignored() -> None:
    client = TestClient(app)
    response = _post_linear(client, {"type": "Reaction", "action": "create"})

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert "Not a Comment or Issue event" in response.json()["reason"]
