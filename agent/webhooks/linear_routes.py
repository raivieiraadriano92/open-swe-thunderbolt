"""Linear webhook HTTP routes."""

import time
from typing import Any

from fastapi import APIRouter

from ..utils.agent_comments import AGENT_COMMENT_MARKER
from . import common
from . import linear as service

router = APIRouter()

# Process-lifetime dedup for Linear issue-label dispatches. Linear can fan the
# same logical "user added the openswe label" out into an Issue.create + one or
# more Issue.update webhooks that all pass ``_thread_exists`` before the first
# background task actually creates the thread, causing duplicate dispatches
# (each one posting another "On it!" comment and interrupting the last run).
# Marking an issue as "recently dispatched" the moment we accept its event
# closes the race for the whole webhook fan-out.
_RECENT_DISPATCH_TTL_S = 30.0
_recent_dispatches: dict[str, float] = {}


def _reserve_dispatch_slot(issue_id: str) -> bool:
    """Atomic mark-and-check: True on first call within the TTL, False after.

    Prunes stale entries in the same pass. Correctness relies on the fact that
    dict membership + assignment run within a single coroutine tick — no
    ``await`` between them — so concurrent webhook handlers cannot both see
    the slot as empty.
    """
    now = time.monotonic()
    for key in [k for k, ts in _recent_dispatches.items() if now - ts > _RECENT_DISPATCH_TTL_S]:
        _recent_dispatches.pop(key, None)
    if issue_id in _recent_dispatches:
        return False
    _recent_dispatches[issue_id] = now
    return True


async def _resolve_repo_for_issue(
    full_issue: dict[str, Any],
    fallback_email: str | None,
) -> dict[str, str] | None:
    """Repo resolution used by the Issue-event path (label triggers).

    Mirrors the resolution ladder used for Comment triggers: dashboard default
    for the triggering user → team/project mapping → team default.
    """
    if fallback_email:
        try:
            profile_repo = await common.get_profile_default_repo(
                await common.resolve_login_from_email_async(fallback_email)
            )
            if profile_repo:
                return profile_repo
        except Exception:  # noqa: BLE001
            common.logger.exception("Failed to apply dashboard default_repo for Linear user")

    team = full_issue.get("team") or {}
    team_name = (team.get("name") or "").strip()
    project = full_issue.get("project") or {}
    project_name = (project.get("name") or "").strip()

    mapped = common.get_repo_config_from_team_mapping(team_name, project_name)
    if mapped:
        return mapped
    return await common.get_team_default_repo()


async def _handle_linear_issue_event(
    payload: dict[str, Any],
    background_tasks: common.BackgroundTasks,
) -> dict[str, str]:
    """Trigger a run when an issue gains the ``openswe`` label."""
    action = payload.get("action")
    if action not in ("create", "update"):
        return {"status": "ignored", "reason": f"Issue action is '{action}', not create/update"}

    data = payload.get("data") or {}
    issue_id = data.get("id", "")
    if not issue_id:
        return {"status": "ignored", "reason": "No issue id in payload"}

    full_issue = await common.fetch_linear_issue_details(issue_id)
    if not full_issue:
        common.logger.warning("Failed to fetch Linear issue %s for label check; ignoring", issue_id)
        return {"status": "ignored", "reason": "Failed to fetch issue details"}

    labels = ((full_issue.get("labels") or {}).get("nodes")) or []
    has_label = any((label.get("name") or "").lower() == common.OPEN_SWE_LABEL for label in labels)
    if not has_label:
        return {
            "status": "ignored",
            "reason": f"Issue does not carry the '{common.OPEN_SWE_LABEL}' label",
        }

    # Idempotency: same thread id ⇒ we already dispatched on this issue.
    # Re-adding the label doesn't re-fire; drop the label and re-add to trigger
    # a fresh conversation on a new issue (or use @openswe in a comment).
    thread_id = common.generate_thread_id_from_issue(issue_id)
    if await common._thread_exists(thread_id):
        return {
            "status": "ignored",
            "reason": "A thread already exists for this issue",
        }

    # Race window: `_thread_exists` yields, so a fan-out of Issue.create +
    # Issue.update for the same label add can both reach here before either
    # background task creates the thread. Reserve the slot atomically to
    # ensure only the first handler proceeds.
    if not _reserve_dispatch_slot(issue_id):
        return {
            "status": "ignored",
            "reason": "Concurrent dispatch already scheduled for this issue",
        }

    fallback_email = (full_issue.get("creator") or {}).get("email") or (
        (full_issue.get("assignee") or {}).get("email")
    )
    repo_config = await _resolve_repo_for_issue(full_issue, fallback_email)
    if not repo_config:
        return {"status": "ignored", "reason": "No default repository configured"}
    if not common._is_repo_allowed(repo_config):
        common.logger.warning(
            "Rejecting Linear webhook: repo '%s/%s' not in allowlist",
            repo_config.get("owner"),
            repo_config.get("name"),
        )
        return {"status": "ignored", "reason": "Repository not in allowlist"}

    issue = dict(full_issue)
    issue["id"] = issue_id
    common.logger.info(
        "Accepted Linear Issue label event for issue '%s' (%s), scheduling background task",
        issue.get("title"),
        issue_id,
    )
    background_tasks.add_task(service.process_linear_issue, issue, repo_config)
    return {
        "status": "accepted",
        "message": f"Processing labeled issue '{issue.get('title')}' for repo "
        f"{repo_config['owner']}/{repo_config['name']}",
    }


@router.post("/webhooks/linear")
async def linear_webhook(  # noqa: PLR0911, PLR0912, PLR0915
    request: common.Request, background_tasks: common.BackgroundTasks
) -> dict[str, str]:
    """Handle Linear webhooks.

    Triggers a new LangGraph run when either an ``@openswe`` comment is posted
    on an issue, or the issue is labeled with ``openswe``.
    """
    common.logger.info("Received Linear webhook")
    body = await request.body()

    signature = request.headers.get("Linear-Signature", "")
    if not common.verify_linear_signature(body, signature, common.LINEAR_WEBHOOK_SECRET):
        common.logger.warning("Invalid webhook signature")
        raise common.HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = common.json.loads(body)
    except common.json.JSONDecodeError:
        common.logger.exception("Failed to parse webhook JSON")
        return {"status": "error", "message": "Invalid JSON"}

    event_type = payload.get("type")
    if event_type == "Issue":
        return await _handle_linear_issue_event(payload, background_tasks)
    if event_type != "Comment":
        common.logger.debug("Ignoring webhook: not a Comment or Issue event")
        return {"status": "ignored", "reason": "Not a Comment or Issue event"}

    action = payload.get("action")
    if action != "create":
        common.logger.debug("Ignoring webhook: action is %s, not create", action)
        return {
            "status": "ignored",
            "reason": f"Comment action is '{action}', only processing 'create'",
        }

    data = payload.get("data", {})

    if data.get("botActor"):
        common.logger.debug("Ignoring webhook: comment is from a bot")
        return {"status": "ignored", "reason": "Comment is from a bot"}

    comment_body = data.get("body", "")
    bot_message_prefixes = [
        AGENT_COMMENT_MARKER,
        "🔐 **GitHub Authentication Required**",
        "✅ **Pull Request Created**",
        "✅ **Pull Request Updated**",
        "**Pull Request Created**",
        "**Pull Request Updated**",
        "🤖 **Agent Response**",
        "❌ **Agent Error**",
    ]
    for prefix in bot_message_prefixes:
        if comment_body.startswith(prefix):
            common.logger.debug("Ignoring webhook: comment is our own bot message")
            return {"status": "ignored", "reason": "Comment is our own bot message"}
    if "@openswe" not in comment_body.lower():
        common.logger.debug("Ignoring webhook: comment doesn't mention @openswe")
        return {"status": "ignored", "reason": "Comment doesn't mention @openswe"}

    issue = data.get("issue", {})
    if not issue:
        common.logger.debug("Ignoring webhook: no issue data in comment")
        return {"status": "ignored", "reason": "No issue data in comment"}

    # Fetch full issue details to get project info (webhook doesn't include it)
    issue_id = issue.get("id", "")
    full_issue = await common.fetch_linear_issue_details(issue_id)
    if not full_issue:
        common.logger.warning("Failed to fetch full issue details, using webhook data")
        full_issue = issue

    repo_config = common.extract_repo_from_text(
        comment_body, default_owner=common.DEFAULT_REPO_OWNER
    )

    if repo_config:
        common.logger.debug(
            "Using repo from comment body: %s/%s",
            repo_config["owner"],
            repo_config["name"],
        )
    else:
        comment_user_email = (data.get("user") or {}).get("email")
        try:
            profile_repo = await common.get_profile_default_repo(
                await common.resolve_login_from_email_async(comment_user_email)
            )
        except Exception:  # noqa: BLE001
            common.logger.exception("Failed to apply dashboard default_repo for Linear user")
            profile_repo = None
        if profile_repo:
            common.logger.info(
                "Applying dashboard default_repo for Linear user %s: %s/%s",
                comment_user_email,
                profile_repo["owner"],
                profile_repo["name"],
            )
            repo_config = profile_repo

    if not repo_config:
        team = full_issue.get("team", {})
        team_name = team.get("name", "") if team else ""
        project = full_issue.get("project")
        project_name = project.get("name", "") if project else ""

        team_identifier = team_name.strip() if team_name else ""
        project_key = project_name.strip() if project_name else ""

        repo_config = common.get_repo_config_from_team_mapping(team_identifier, project_key)

        common.logger.debug(
            "Team/project lookup result",
            extra={
                "team_name": team_identifier,
                "project_name": project_key,
                "repo_config": repo_config,
            },
        )

    if not repo_config:
        repo_config = await common.get_team_default_repo()

    if not repo_config:
        return {"status": "ignored", "reason": "No default repository configured"}

    if not common._is_repo_allowed(repo_config):
        common.logger.warning(
            "Rejecting Linear webhook: repo '%s/%s' not in allowlist",
            repo_config.get("owner"),
            repo_config.get("name"),
        )
        return {"status": "ignored", "reason": "Repository not in allowlist"}

    repo_owner = repo_config["owner"]
    repo_name = repo_config["name"]

    issue["triggering_comment"] = comment_body
    issue["triggering_comment_id"] = data.get("id", "")
    comment_user = data.get("user", {})
    if comment_user:
        issue["comment_author"] = comment_user

    common.logger.info(
        "Accepted webhook for issue '%s' (%s), scheduling background task",
        issue.get("title"),
        issue.get("id"),
    )
    background_tasks.add_task(service.process_linear_issue, issue, repo_config)

    return {
        "status": "accepted",
        "message": f"Processing issue '{issue.get('title')}' for repo {repo_owner}/{repo_name}",
    }


@router.get("/webhooks/linear")
async def linear_webhook_verify() -> dict[str, str]:
    """Verify endpoint for Linear webhook setup."""
    return {"status": "ok", "message": "Linear webhook endpoint is active"}
