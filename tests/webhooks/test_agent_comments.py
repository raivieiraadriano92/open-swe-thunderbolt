"""Tests for the shared agent-comment formatter and outbound comment prefixing."""

from __future__ import annotations

from typing import Any

import pytest

from agent.utils import agent_comments, github_comments
from agent.utils import linear as linear_utils
from agent.utils.agent_comments import AGENT_COMMENT_MARKER, format_agent_comment, is_agent_comment


def test_marker_prepended_when_no_trace_url_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_comments, "get_langsmith_trace_url", lambda _tid: None)
    out = format_agent_comment("Body text", thread_id="t-1")
    assert out.startswith(AGENT_COMMENT_MARKER)
    assert "Body text" in out
    assert "trace" not in out


def test_marker_includes_trace_link_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agent_comments,
        "get_langsmith_trace_url",
        lambda _tid: "https://smith.langchain.com/o/x/projects/p/y/t/z",
    )
    out = format_agent_comment("Body", thread_id="t-1")
    assert out.startswith(f"{AGENT_COMMENT_MARKER} · [trace](")
    assert "\n\nBody" in out


def test_marker_alone_when_body_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_comments, "get_langsmith_trace_url", lambda _tid: None)
    assert format_agent_comment("", thread_id=None) == AGENT_COMMENT_MARKER


def test_is_agent_comment_detects_marker() -> None:
    assert is_agent_comment(f"{AGENT_COMMENT_MARKER}\n\nBody")
    assert is_agent_comment(f"  {AGENT_COMMENT_MARKER} · [trace](x)\n\nBody")
    assert not is_agent_comment("Some other bot message")
    assert not is_agent_comment("")


def test_comment_on_linear_issue_prefixes_body(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_graphql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        captured["body"] = variables["body"]
        return {"commentCreate": {"success": True}}

    monkeypatch.setattr(linear_utils, "_graphql_request", fake_graphql)
    monkeypatch.setattr(agent_comments, "get_langsmith_trace_url", lambda _tid: None)

    import asyncio

    ok = asyncio.run(linear_utils.comment_on_linear_issue("issue-1", "Working on it"))
    assert ok is True
    assert captured["body"].startswith(AGENT_COMMENT_MARKER)
    assert "Working on it" in captured["body"]


def test_post_github_comment_prefixes_body(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, *a, **kw) -> None:  # noqa: ARG002
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *a: Any) -> None:  # noqa: ARG002
            return None

        async def post(self, url: str, *, json: dict, headers: dict) -> FakeResponse:  # noqa: ARG002
            captured["body"] = json["body"]
            return FakeResponse()

    captured: dict[str, Any] = {}
    monkeypatch.setattr(github_comments.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(agent_comments, "get_langsmith_trace_url", lambda _tid: None)

    import asyncio

    ok = asyncio.run(
        github_comments.post_github_comment(
            {"owner": "acme", "name": "repo"}, 42, "Hi", token="tok"
        )
    )
    assert ok is True
    assert captured["body"].startswith(AGENT_COMMENT_MARKER)
    assert "Hi" in captured["body"]


def test_post_github_issue_trace_comment_uses_on_it_body(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_post(repo_config, issue_number, body, *, token, thread_id=None):
        captured["issue_number"] = issue_number
        captured["body"] = body
        captured["thread_id"] = thread_id
        return True

    monkeypatch.setattr(github_comments, "post_github_comment", fake_post)

    import asyncio

    ok = asyncio.run(
        github_comments.post_github_issue_trace_comment(
            {"owner": "acme", "name": "repo"}, 42, "thread-1", token="tok"
        )
    )
    assert ok is True
    assert captured["body"] == "On it!"
    assert captured["thread_id"] == "thread-1"


