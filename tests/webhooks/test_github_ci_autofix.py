"""Tests for the GitHub CI auto-fix webhook path.

Covers: happy path dispatches a run, per-PR opt-out disables it, failures
already red on the base branch are skipped, dedup blocks a duplicate event
for the same (PR, head_sha), and CI events for PRs the agent didn't open are
ignored.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from agent.api.app import app
from agent.webhooks import common as webhook_common
from agent.webhooks import github as github_webhooks

_SECRET = "test-github-secret"


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _post_github(client: TestClient, event_type: str, payload: dict) -> Any:
    body = json.dumps(payload, separators=(",", ":")).encode()
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": event_type,
            "X-Hub-Signature-256": _sign(body),
            "Content-Type": "application/json",
        },
    )


def _check_run_payload(
    *,
    action: str = "completed",
    conclusion: str = "failure",
    head_branch: str = "openswe/fix-thing",
    head_sha: str = "deadbeefcafe0001",
) -> dict:
    return {
        "action": action,
        "check_run": {
            "status": "completed",
            "conclusion": conclusion,
            "head_sha": head_sha,
            "check_suite": {"head_branch": head_branch},
        },
        "repository": {"owner": {"login": "acme"}, "name": "app"},
        "sender": {"login": "gh-actions"},
    }


@pytest.fixture(autouse=True)
def _webhook_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webhook_common, "GITHUB_WEBHOOK_SECRET", _SECRET)


@pytest.fixture(autouse=True)
def _clear_dedup_state() -> None:
    """The dedup dict is process-lifetime; wipe it between tests."""
    github_webhooks._recent_ci_autofix.clear()


@pytest.fixture(autouse=True)
def _stub_token(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_token(*_a: Any, **_kw: Any) -> tuple[str, str]:
        return ("stub-app-token", "2099-01-01T00:00:00Z")

    monkeypatch.setattr(webhook_common, "_reviewer_token_for_repo", fake_token)


def _stub_dispatch(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    async def fake_dispatch(
        thread_id: str,
        content: Any,
        configurable: dict[str, Any],
        *,
        source: str,
        assistant_id: str = "agent",
        metadata: dict[str, Any] | None = None,
        client: Any = None,
    ) -> dict[str, Any]:
        captured["thread_id"] = thread_id
        captured["content"] = content
        captured["configurable"] = configurable
        captured["source"] = source
        captured["metadata"] = metadata
        return {"run_id": "run-1"}

    monkeypatch.setattr(webhook_common, "dispatch_agent_run", fake_dispatch)


def _stub_thread_search(monkeypatch: pytest.MonkeyPatch, threads: list[dict[str, Any]]) -> None:
    class FakeThreads:
        async def search(self, *, metadata: dict[str, Any], limit: int) -> list[dict[str, Any]]:  # noqa: ARG002
            return threads

    class FakeClient:
        threads = FakeThreads()

    def fake_get_client(*_a: Any, **_kw: Any) -> Any:
        return FakeClient()

    monkeypatch.setattr(webhook_common, "get_client", fake_get_client)


def test_check_run_failure_dispatches_autofix(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_fetch_pr(*, owner: str, repo: str, branch: str, token: str) -> dict[str, Any]:  # noqa: ARG001
        return {
            "number": 42,
            "html_url": "https://github.com/acme/app/pull/42",
            "base": {"sha": "base123", "ref": "main"},
        }

    async def fake_failing_checks(
        *, owner: str, repo: str, ref: str, token: str
    ) -> list[dict[str, Any]]:  # noqa: ARG001
        return [{"name": "typecheck", "conclusion": "failure", "details_url": "https://ci/run/1"}]

    async def fake_failing_statuses(*_a: Any, **_kw: Any) -> list[dict[str, Any]]:
        return []

    async def fake_base_names(*_a: Any, **_kw: Any) -> set[str]:
        return set()

    async def fake_autofix_disabled(*_a: Any, **_kw: Any) -> bool:
        return False

    monkeypatch.setattr(github_webhooks, "fetch_open_pr_for_branch", fake_fetch_pr)
    monkeypatch.setattr(github_webhooks, "list_failing_check_runs", fake_failing_checks)
    monkeypatch.setattr(github_webhooks, "list_failing_statuses", fake_failing_statuses)
    monkeypatch.setattr(github_webhooks, "names_failing_on_base", fake_base_names)
    monkeypatch.setattr(github_webhooks, "is_pr_autofix_disabled", fake_autofix_disabled)

    _stub_thread_search(
        monkeypatch,
        [
            {
                "thread_id": "agent-thread-abc",
                "metadata": {
                    "kind": "agent",
                    "pr_url": "https://github.com/acme/app/pull/42",
                    "github_login": "octocat",
                    "user_email": "octo@example.com",
                },
            }
        ],
    )
    _stub_dispatch(monkeypatch, captured)

    client = TestClient(app)
    resp = _post_github(client, "check_run", _check_run_payload())

    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    assert captured["thread_id"] == "agent-thread-abc"
    assert captured["source"] == "github_ci"
    assert "typecheck" in captured["content"]
    assert captured["configurable"]["pr_number"] == 42
    assert captured["configurable"]["ci_head_sha"] == "deadbeefcafe0001"
    # Identity preserved for the fix commit.
    assert captured["configurable"]["github_login"] == "octocat"
    assert captured["configurable"]["user_email"] == "octo@example.com"


def test_per_pr_autofix_disable_blocks_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch_pr(**_kw: Any) -> dict[str, Any]:
        return {
            "number": 42,
            "html_url": "https://github.com/acme/app/pull/42",
            "base": {"sha": "b"},
        }

    async def fake_autofix_disabled(*_a: Any, **_kw: Any) -> bool:
        return True

    monkeypatch.setattr(github_webhooks, "fetch_open_pr_for_branch", fake_fetch_pr)
    monkeypatch.setattr(github_webhooks, "is_pr_autofix_disabled", fake_autofix_disabled)
    monkeypatch.setattr(
        webhook_common,
        "dispatch_agent_run",
        AsyncMock(side_effect=AssertionError("should not dispatch")),
    )

    client = TestClient(app)
    resp = _post_github(client, "check_run", _check_run_payload())

    assert resp.status_code == 200
    # Router accepted the event; the handler short-circuits after the flag check.
    assert resp.json()["status"] == "accepted"


def test_base_inherited_failures_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch_pr(**_kw: Any) -> dict[str, Any]:
        return {
            "number": 42,
            "html_url": "https://github.com/acme/app/pull/42",
            "base": {"sha": "base"},
        }

    async def fake_failing_checks(**_kw: Any) -> list[dict[str, Any]]:
        return [{"name": "lint", "conclusion": "failure", "details_url": ""}]

    async def fake_failing_statuses(**_kw: Any) -> list[dict[str, Any]]:
        return []

    async def fake_base_names(**_kw: Any) -> set[str]:
        return {"lint"}  # Already red on base — pre-existing, not the PR's fault.

    async def fake_autofix_disabled(*_a: Any, **_kw: Any) -> bool:
        return False

    monkeypatch.setattr(github_webhooks, "fetch_open_pr_for_branch", fake_fetch_pr)
    monkeypatch.setattr(github_webhooks, "list_failing_check_runs", fake_failing_checks)
    monkeypatch.setattr(github_webhooks, "list_failing_statuses", fake_failing_statuses)
    monkeypatch.setattr(github_webhooks, "names_failing_on_base", fake_base_names)
    monkeypatch.setattr(github_webhooks, "is_pr_autofix_disabled", fake_autofix_disabled)

    dispatch_calls = 0

    async def fake_dispatch(*_a: Any, **_kw: Any) -> dict[str, Any]:
        nonlocal dispatch_calls
        dispatch_calls += 1
        return {"run_id": "x"}

    monkeypatch.setattr(webhook_common, "dispatch_agent_run", fake_dispatch)

    client = TestClient(app)
    resp = _post_github(client, "check_run", _check_run_payload())

    assert resp.status_code == 200
    assert dispatch_calls == 0


def test_dedup_blocks_duplicate_events_for_same_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch_pr(**_kw: Any) -> dict[str, Any]:
        return {
            "number": 42,
            "html_url": "https://github.com/acme/app/pull/42",
            "base": {"sha": "b"},
        }

    async def fake_failing_checks(**_kw: Any) -> list[dict[str, Any]]:
        return [{"name": "test", "conclusion": "failure", "details_url": ""}]

    async def fake_failing_statuses(**_kw: Any) -> list[dict[str, Any]]:
        return []

    async def fake_base_names(**_kw: Any) -> set[str]:
        return set()

    async def fake_autofix_disabled(*_a: Any, **_kw: Any) -> bool:
        return False

    monkeypatch.setattr(github_webhooks, "fetch_open_pr_for_branch", fake_fetch_pr)
    monkeypatch.setattr(github_webhooks, "list_failing_check_runs", fake_failing_checks)
    monkeypatch.setattr(github_webhooks, "list_failing_statuses", fake_failing_statuses)
    monkeypatch.setattr(github_webhooks, "names_failing_on_base", fake_base_names)
    monkeypatch.setattr(github_webhooks, "is_pr_autofix_disabled", fake_autofix_disabled)

    _stub_thread_search(
        monkeypatch,
        [
            {
                "thread_id": "t-1",
                "metadata": {"kind": "agent", "pr_url": "https://github.com/acme/app/pull/42"},
            }
        ],
    )

    dispatch_count = {"n": 0}

    async def fake_dispatch(*_a: Any, **_kw: Any) -> dict[str, Any]:
        dispatch_count["n"] += 1
        return {"run_id": "x"}

    monkeypatch.setattr(webhook_common, "dispatch_agent_run", fake_dispatch)

    client = TestClient(app)
    payload = _check_run_payload()
    resp1 = _post_github(client, "check_run", payload)
    resp2 = _post_github(
        client,
        "workflow_run",
        {
            "action": "completed",
            "workflow_run": {
                "status": "completed",
                "conclusion": "failure",
                "head_sha": payload["check_run"]["head_sha"],
                "head_branch": payload["check_run"]["check_suite"]["head_branch"],
            },
            "repository": payload["repository"],
            "sender": payload["sender"],
        },
    )

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    # Same commit, two fan-out events — only one auto-fix run should dispatch.
    assert dispatch_count["n"] == 1


def test_no_agent_thread_means_no_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI on PRs the agent didn't open should be ignored (no metadata match)."""

    async def fake_fetch_pr(**_kw: Any) -> dict[str, Any]:
        return {"number": 7, "html_url": "https://github.com/acme/app/pull/7", "base": {"sha": "b"}}

    async def fake_failing_checks(**_kw: Any) -> list[dict[str, Any]]:
        return [{"name": "typecheck", "conclusion": "failure", "details_url": ""}]

    async def fake_failing_statuses(**_kw: Any) -> list[dict[str, Any]]:
        return []

    async def fake_base_names(**_kw: Any) -> set[str]:
        return set()

    async def fake_autofix_disabled(*_a: Any, **_kw: Any) -> bool:
        return False

    monkeypatch.setattr(github_webhooks, "fetch_open_pr_for_branch", fake_fetch_pr)
    monkeypatch.setattr(github_webhooks, "list_failing_check_runs", fake_failing_checks)
    monkeypatch.setattr(github_webhooks, "list_failing_statuses", fake_failing_statuses)
    monkeypatch.setattr(github_webhooks, "names_failing_on_base", fake_base_names)
    monkeypatch.setattr(github_webhooks, "is_pr_autofix_disabled", fake_autofix_disabled)
    _stub_thread_search(monkeypatch, [])  # No agent thread for this PR.

    dispatch_count = {"n": 0}

    async def fake_dispatch(*_a: Any, **_kw: Any) -> dict[str, Any]:
        dispatch_count["n"] += 1
        return {"run_id": "x"}

    monkeypatch.setattr(webhook_common, "dispatch_agent_run", fake_dispatch)

    client = TestClient(app)
    resp = _post_github(client, "check_run", _check_run_payload())

    assert resp.status_code == 200
    assert dispatch_count["n"] == 0


def test_reviewer_thread_is_not_treated_as_agent_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """threads.search may return the reviewer thread too — we must skip it."""

    async def fake_fetch_pr(**_kw: Any) -> dict[str, Any]:
        return {"number": 9, "html_url": "https://github.com/acme/app/pull/9", "base": {"sha": "b"}}

    async def fake_failing_checks(**_kw: Any) -> list[dict[str, Any]]:
        return [{"name": "lint", "conclusion": "failure", "details_url": ""}]

    async def fake_failing_statuses(**_kw: Any) -> list[dict[str, Any]]:
        return []

    async def fake_base_names(**_kw: Any) -> set[str]:
        return set()

    async def fake_autofix_disabled(*_a: Any, **_kw: Any) -> bool:
        return False

    monkeypatch.setattr(github_webhooks, "fetch_open_pr_for_branch", fake_fetch_pr)
    monkeypatch.setattr(github_webhooks, "list_failing_check_runs", fake_failing_checks)
    monkeypatch.setattr(github_webhooks, "list_failing_statuses", fake_failing_statuses)
    monkeypatch.setattr(github_webhooks, "names_failing_on_base", fake_base_names)
    monkeypatch.setattr(github_webhooks, "is_pr_autofix_disabled", fake_autofix_disabled)
    _stub_thread_search(
        monkeypatch,
        [
            {
                "thread_id": "reviewer-thread",
                "metadata": {
                    "kind": webhook_common.REVIEWER_THREAD_KIND,
                    "pr_url": "https://github.com/acme/app/pull/9",
                },
            }
        ],
    )

    dispatch_count = {"n": 0}

    async def fake_dispatch(*_a: Any, **_kw: Any) -> dict[str, Any]:
        dispatch_count["n"] += 1
        return {"run_id": "x"}

    monkeypatch.setattr(webhook_common, "dispatch_agent_run", fake_dispatch)

    client = TestClient(app)
    resp = _post_github(client, "check_run", _check_run_payload())

    assert resp.status_code == 200
    assert dispatch_count["n"] == 0


def test_reserve_ci_autofix_slot_atomic() -> None:
    """Second reservation for the same key returns False; different key returns True."""
    github_webhooks._recent_ci_autofix.clear()

    assert github_webhooks._reserve_ci_autofix_slot("acme", "app", 1, "sha1") is True
    assert github_webhooks._reserve_ci_autofix_slot("acme", "app", 1, "sha1") is False
    # Different sha → new slot.
    assert github_webhooks._reserve_ci_autofix_slot("acme", "app", 1, "sha2") is True
    # Different PR → new slot.
    assert github_webhooks._reserve_ci_autofix_slot("acme", "app", 2, "sha1") is True


def test_non_failing_ci_event_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A completed-success check-run must not trigger auto-fix."""
    dispatch_count = {"n": 0}

    async def fake_dispatch(*_a: Any, **_kw: Any) -> dict[str, Any]:
        dispatch_count["n"] += 1
        return {"run_id": "x"}

    monkeypatch.setattr(webhook_common, "dispatch_agent_run", fake_dispatch)

    client = TestClient(app)
    resp = _post_github(client, "check_run", _check_run_payload(conclusion="success"))

    assert resp.status_code == 200
    assert dispatch_count["n"] == 0
