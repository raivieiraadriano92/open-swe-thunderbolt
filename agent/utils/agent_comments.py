"""Standard marker + formatter for agent-authored comments (Linear + GitHub).

The marker at the top of every outbound comment (`🤖 **Open SWE**`) serves two
purposes:

1. Humans can visually distinguish agent-authored comments from human replies
   in issue threads.
2. Our own webhook handlers can loop-detect via ``bot_message_prefixes`` — a
   comment starting with the marker is our own and must not re-trigger a run.

Every outbound comment path (webhook auto-comments, the `linear_comment` tool,
completion webhook failure replies, sandbox circuit-breaker notices) routes
through :func:`format_agent_comment` so the marker is applied uniformly.
"""

from __future__ import annotations

from .langsmith import get_langsmith_trace_url

AGENT_COMMENT_MARKER = "🤖 **Open SWE**"


def format_agent_comment(body: str, *, thread_id: str | None = None) -> str:
    """Prepend the standard agent marker to ``body``.

    When ``thread_id`` is provided and LangSmith tracing is configured, a
    ``trace`` link is appended to the marker line.
    """
    header_parts = [AGENT_COMMENT_MARKER]
    if thread_id:
        trace_url = get_langsmith_trace_url(thread_id)
        if trace_url:
            header_parts.append(f"[trace]({trace_url})")
    header = " · ".join(header_parts)
    body = (body or "").strip()
    return f"{header}\n\n{body}" if body else header


def is_agent_comment(body: str) -> bool:
    """Whether ``body`` is a comment authored by us (starts with the marker)."""
    return (body or "").lstrip().startswith(AGENT_COMMENT_MARKER)
