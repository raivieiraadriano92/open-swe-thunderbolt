# THU-696 PoC — Todo

Companion checklist to [`THUNDERBOLT.md`](./THUNDERBOLT.md). Items are grouped by whether we can act now or are waiting on something. Check off as PRs / config changes land.

## Immediate (can start now)

- [x] **Attach Render Persistent Disk to `/app/.langgraph_api`** — done; verified in Round 7 (user-mapping seed survived restart and was read back).

- [x] **Document the LangGraph Platform licensing finding** — captured in `THUNDERBOLT.md` § *LangGraph Platform licensing*. Chose Option 1 (persistent disk); Options 2 / 4 flagged for real production.

- [x] **Deploy dashboard UI (Render Static Site)** — live at `open-swe-poc-ui.onrender.com`. SPA rewrite rule + compile-time `VITE_DASHBOARD_API_BASE_URL` in place. Backend OAuth wiring correct (`redirect_uri` matches App callback).

- [x] **Verify GitHub-trigger flow end-to-end** — Round 7: `@openswe` comment on issue #5 → PR #6 in 95s. Trigger surface count now 2 (Linear + GitHub).

- [ ] **Run 1–2 varied tickets beyond README edits**
  - Current cost/latency data is entirely from trivial README-touching tasks.
  - Need: (a) a small bug fix requiring multi-file reads + a repro test; (b) a small typed refactor that touches 3–5 files with cross-file type consistency.
  - Both against `thunderbird/thunderbolt-sandbox`.
  - Track per run: cost, wall-time, patches needed, PR quality (matches our review bar?).

## Waiting on org owner

- [x] **`GITHUB_APP_CLIENT_SECRET` + callback URL set** — done. OAuth `redirect_uri` verified correct at `/dashboard/api/auth/login` (302 → GitHub OAuth authorize with the right params).

- [x] **GitHub App webhook activated** — done. Recent deliveries returning HTTP 200. `issue_comment` and `issues.opened` events routed correctly.

- [ ] **Dashboard login for outside collaborators** — blocked. `raivieiraadriano92` is only an outside collaborator on 7 `thunderbird` repos, not an org member. GitHub App is restricted to "Only on this account" → OAuth returns 404 for non-members. Owner (Sancus) will add to org after billing changes settle. Alternative: flip App to "Any account" + set `ALLOWED_GITHUB_ORGS=` empty on backend.

## After login is live (unlocked by client secret)

- [ ] **Try plan-approval flow and capture cost-control screenshot**
  - The #1 differentiator over LangGraph Studio — would have caught our Round-1 $20 incident.
  - Trigger a Linear run, wait for plan, visit `/agents/$threadId/plan`, approve/reject.
  - Screenshot for the final report as "why adopt" evidence.

- [ ] **Trigger review-style analyzer on `thunderbolt-sandbox`**
  - Depends on Render Persistent Disk being attached (Store persistence needed to see the synthesized prompt survive restart).
  - Dashboard → click "Analyze style" for the repo → wait for the analyzer graph to finish.
  - Compare the synthesized prompt to our real `CLAUDE.md`. How close does it come?
  - Novel capability worth calling out in the report as "consider adapting."

## Nice-to-have (probably skip for PoC)

- [ ] **Switch OpenRouter → direct Anthropic** — removes patch #2 (cache_control injection hack). Cleaner code, better cost numbers, but OpenRouter already proved the pluggable-providers story. Do this only if it makes the report cleaner.

- [ ] **AWS ECS / Fargate sandbox** — Open SWE has no built-in AWS provider; would need a ~200 LOC custom factory (`agent/integrations/aws_ecs.py`). Only pursue if there's a business reason (data residency, Daytona pricing at scale). Otherwise Daytona is fine.

- [ ] **Datadog MCP** — only if Thunderbolt sends logs there. Would let the agent read prod telemetry when debugging: "what errors did we see in prod for this endpoint?"

## Final deliverable

- [ ] **Write final THU-696 adopt/adapt/discard report**
  - Depends on all prior tasks.
  - Post as a Linear comment on THU-696.
  - Structure:
    1. TL;DR recommendation.
    2. What worked / what didn't.
    3. 8-patch summary + maintenance burden going forward.
    4. Cost data across ticket variety.
    5. Production-readiness matrix (persistence via disk ✓, sandbox ✓, teardown ✓, dashboard ✓, plan-approval ✓, review-style ✓; Postgres/multi-instance needs custom wrapper or paid license).
    6. Risks / caveats.
    7. Next steps if adopted.
