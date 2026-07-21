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


@pytest.fixture(autouse=True)
def _default_stub_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the langgraph client to an in-memory fake — tests that need a
    specific thread search result can call ``_stub_thread_search`` to override.

    Without this, ``_find_agent_thread_for_pr`` and ``_set_ci_autofix_attempts``
    would hit the real langgraph URL and hang on connect timeouts.
    """
    _stub_thread_search(monkeypatch, [])


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


def _stub_thread_search(
    monkeypatch: pytest.MonkeyPatch, threads: list[dict[str, Any]]
) -> dict[str, Any]:
    """Install a fake langgraph client. Returns a dict that captures every
    ``threads.update`` call (keyed by thread_id → merged metadata), so tests
    can assert the attempt counter was written correctly.
    """
    updated_metadata: dict[str, Any] = {}

    class FakeThreads:
        async def search(self, *, metadata: dict[str, Any], limit: int) -> list[dict[str, Any]]:  # noqa: ARG002
            return threads

        async def update(self, *, thread_id: str, metadata: dict[str, Any]) -> None:
            existing = updated_metadata.setdefault(thread_id, {})
            existing.update(metadata)

    fake_threads = FakeThreads()

    class FakeClient:
        threads = fake_threads

    def fake_get_client(*_a: Any, **_kw: Any) -> Any:
        return FakeClient()

    monkeypatch.setattr(webhook_common, "get_client", fake_get_client)
    return updated_metadata


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
    """A completed-success check-run must not trigger auto-fix.

    (This is the pre-attempt-counter behavior: with no agent thread stubbed,
    ``_find_agent_thread_for_pr`` short-circuits so we never even fetch the
    failing state.)
    """
    dispatch_count = {"n": 0}

    async def fake_dispatch(*_a: Any, **_kw: Any) -> dict[str, Any]:
        dispatch_count["n"] += 1
        return {"run_id": "x"}

    async def fake_fetch_pr(**_kw: Any) -> dict[str, Any]:
        return {
            "number": 42,
            "html_url": "https://github.com/acme/app/pull/42",
            "base": {"sha": "b"},
        }

    monkeypatch.setattr(github_webhooks, "fetch_open_pr_for_branch", fake_fetch_pr)
    monkeypatch.setattr(webhook_common, "dispatch_agent_run", fake_dispatch)

    client = TestClient(app)
    resp = _post_github(client, "check_run", _check_run_payload(conclusion="success"))

    assert resp.status_code == 200
    assert dispatch_count["n"] == 0


def _fakes_for_gate_tests(
    monkeypatch: pytest.MonkeyPatch, *, autofix_disabled: bool = False
) -> None:
    """Wire the minimal fakes for tests exercising the attempt-gate logic."""

    async def fake_fetch_pr(**_kw: Any) -> dict[str, Any]:
        return {
            "number": 42,
            "html_url": "https://github.com/acme/app/pull/42",
            "base": {"sha": "base"},
        }

    async def fake_failing_checks(**_kw: Any) -> list[dict[str, Any]]:
        return [{"name": "typecheck", "conclusion": "failure", "details_url": ""}]

    async def fake_failing_statuses(**_kw: Any) -> list[dict[str, Any]]:
        return []

    async def fake_base_names(**_kw: Any) -> set[str]:
        return set()

    async def fake_autofix_disabled(*_a: Any, **_kw: Any) -> bool:
        return autofix_disabled

    monkeypatch.setattr(github_webhooks, "fetch_open_pr_for_branch", fake_fetch_pr)
    monkeypatch.setattr(github_webhooks, "list_failing_check_runs", fake_failing_checks)
    monkeypatch.setattr(github_webhooks, "list_failing_statuses", fake_failing_statuses)
    monkeypatch.setattr(github_webhooks, "names_failing_on_base", fake_base_names)
    monkeypatch.setattr(github_webhooks, "is_pr_autofix_disabled", fake_autofix_disabled)


def test_attempt_counter_increments_on_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each dispatch must bump the ``ci_autofix_attempts`` metadata key by 1."""
    _fakes_for_gate_tests(monkeypatch)
    captured_updates = _stub_thread_search(
        monkeypatch,
        [
            {
                "thread_id": "t-1",
                "metadata": {
                    "kind": "agent",
                    "pr_url": "https://github.com/acme/app/pull/42",
                    "ci_autofix_attempts": 1,  # Already one attempt on the record.
                },
            }
        ],
    )

    async def fake_dispatch(*_a: Any, **_kw: Any) -> dict[str, Any]:
        return {"run_id": "run"}

    monkeypatch.setattr(webhook_common, "dispatch_agent_run", fake_dispatch)

    client = TestClient(app)
    resp = _post_github(client, "check_run", _check_run_payload())
    assert resp.status_code == 200

    assert captured_updates.get("t-1", {}).get("ci_autofix_attempts") == 2


def test_escalation_when_max_attempts_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    """After MAX attempts, no more dispatch — post comment + disable auto-fix."""
    _fakes_for_gate_tests(monkeypatch)
    _stub_thread_search(
        monkeypatch,
        [
            {
                "thread_id": "t-1",
                "metadata": {
                    "kind": "agent",
                    "pr_url": "https://github.com/acme/app/pull/42",
                    "github_login": "octocat",
                    "ci_autofix_attempts": github_webhooks.CI_AUTOFIX_MAX_ATTEMPTS,
                },
            }
        ],
    )

    dispatch_count = {"n": 0}
    comment_calls: list[dict[str, Any]] = []
    disable_calls: list[tuple[str, str, int, bool]] = []

    async def fake_dispatch(*_a: Any, **_kw: Any) -> dict[str, Any]:
        dispatch_count["n"] += 1
        return {"run_id": "x"}

    async def fake_comment(
        repo_config: dict[str, str],
        issue_number: int,
        body: str,
        *,
        token: str,
        thread_id: str | None = None,
    ) -> bool:
        comment_calls.append(
            {
                "repo": repo_config,
                "pr": issue_number,
                "body": body,
                "thread_id": thread_id,
            }
        )
        return True

    async def fake_disable(owner: str, repo: str, pr_number: int, disabled: bool) -> None:
        disable_calls.append((owner, repo, pr_number, disabled))

    monkeypatch.setattr(webhook_common, "dispatch_agent_run", fake_dispatch)
    monkeypatch.setattr(github_webhooks, "post_github_comment", fake_comment)
    monkeypatch.setattr(github_webhooks, "set_pr_autofix_disabled", fake_disable)

    client = TestClient(app)
    resp = _post_github(client, "check_run", _check_run_payload())
    assert resp.status_code == 200

    assert dispatch_count["n"] == 0
    assert len(comment_calls) == 1
    assert "3 automatic fix attempts" in comment_calls[0]["body"]
    assert "@octocat" in comment_calls[0]["body"]
    assert disable_calls == [("acme", "app", 42, True)]


def test_reset_counter_on_green_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """A completed CI event with no failing checks resets the attempt counter."""

    async def fake_fetch_pr(**_kw: Any) -> dict[str, Any]:
        return {
            "number": 42,
            "html_url": "https://github.com/acme/app/pull/42",
            "base": {"sha": "b"},
        }

    async def fake_failing_checks(**_kw: Any) -> list[dict[str, Any]]:
        return []  # Everything green.

    async def fake_failing_statuses(**_kw: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(github_webhooks, "fetch_open_pr_for_branch", fake_fetch_pr)
    monkeypatch.setattr(github_webhooks, "list_failing_check_runs", fake_failing_checks)
    monkeypatch.setattr(github_webhooks, "list_failing_statuses", fake_failing_statuses)

    captured_updates = _stub_thread_search(
        monkeypatch,
        [
            {
                "thread_id": "t-green",
                "metadata": {
                    "kind": "agent",
                    "pr_url": "https://github.com/acme/app/pull/42",
                    "ci_autofix_attempts": 2,  # Prior failure streak worth resetting.
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
    resp = _post_github(client, "check_run", _check_run_payload(conclusion="success"))
    assert resp.status_code == 200

    assert dispatch_count["n"] == 0
    assert captured_updates.get("t-green", {}).get("ci_autofix_attempts") == 0


def test_green_ci_with_zero_attempts_does_not_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op path: a green event on a PR with no prior attempts shouldn't
    write to the thread — saves an update call and avoids spurious warnings.
    """

    async def fake_fetch_pr(**_kw: Any) -> dict[str, Any]:
        return {
            "number": 42,
            "html_url": "https://github.com/acme/app/pull/42",
            "base": {"sha": "b"},
        }

    async def fake_failing_checks(**_kw: Any) -> list[dict[str, Any]]:
        return []

    async def fake_failing_statuses(**_kw: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(github_webhooks, "fetch_open_pr_for_branch", fake_fetch_pr)
    monkeypatch.setattr(github_webhooks, "list_failing_check_runs", fake_failing_checks)
    monkeypatch.setattr(github_webhooks, "list_failing_statuses", fake_failing_statuses)

    captured_updates = _stub_thread_search(
        monkeypatch,
        [
            {
                "thread_id": "t-fresh",
                "metadata": {
                    "kind": "agent",
                    "pr_url": "https://github.com/acme/app/pull/42",
                    # No ci_autofix_attempts set → counter is 0.
                },
            }
        ],
    )

    client = TestClient(app)
    resp = _post_github(client, "check_run", _check_run_payload(conclusion="success"))
    assert resp.status_code == 200

    assert "t-fresh" not in captured_updates


def test_in_progress_ci_event_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """``status`` field != completed → we don't touch the thread at all."""

    async def fake_fetch_pr(**_kw: Any) -> dict[str, Any]:
        raise AssertionError("should not reach fetch_open_pr_for_branch")

    monkeypatch.setattr(github_webhooks, "fetch_open_pr_for_branch", fake_fetch_pr)

    payload = _check_run_payload()
    payload["check_run"]["status"] = "in_progress"

    client = TestClient(app)
    resp = _post_github(client, "check_run", payload)
    assert resp.status_code == 200
    # Handler exits early — no error, no fetch.


def test_final_attempt_prompt_mentions_last_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Nth (last) attempt's prompt tells the agent to explain-and-stop
    rather than try another speculative fix.
    """
    _fakes_for_gate_tests(monkeypatch)
    _stub_thread_search(
        monkeypatch,
        [
            {
                "thread_id": "t-last",
                "metadata": {
                    "kind": "agent",
                    "pr_url": "https://github.com/acme/app/pull/42",
                    # attempts=MAX-1 means the run we're about to dispatch IS
                    # the final attempt (attempt=MAX).
                    "ci_autofix_attempts": github_webhooks.CI_AUTOFIX_MAX_ATTEMPTS - 1,
                },
            }
        ],
    )

    captured: dict[str, Any] = {}

    async def fake_dispatch(
        thread_id: str,
        content: Any,
        configurable: dict[str, Any],
        **_kw: Any,
    ) -> dict[str, Any]:
        captured["content"] = content
        return {"run_id": "x"}

    monkeypatch.setattr(webhook_common, "dispatch_agent_run", fake_dispatch)

    client = TestClient(app)
    resp = _post_github(client, "check_run", _check_run_payload())
    assert resp.status_code == 200

    assert "final auto-fix attempt" in captured["content"]
    assert (
        f"attempt {github_webhooks.CI_AUTOFIX_MAX_ATTEMPTS} of {github_webhooks.CI_AUTOFIX_MAX_ATTEMPTS}"
        in captured["content"]
    )
